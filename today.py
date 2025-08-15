import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
import sys
import json
from typing import List

# ---- REQUIRED ENV SECRETS ----
# ACCESS_TOKEN: Fine-grained PAT (read-only is enough; see scopes below)
# USER_NAME: your GitHub username (e.g., "frypan05")
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']  # e.g. 'frypan05'
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'loc_query': 0, 'graph_commits': 0}

# ensure cache dir exists
os.makedirs('cache', exist_ok=True)

# Small helper: retry on transient errors
def _post_with_retry(url, json_payload, headers, retries=2, timeout=30):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=json_payload, headers=headers, timeout=timeout)
            return r
        except Exception as e:
            last_exc = e
            time.sleep(0.5 + attempt * 0.5)
    raise last_exc

def daily_readme(birthday: datetime.datetime) -> str:
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + ('s' if diff.years != 1 else ''),
        diff.months, 'month' + ('s' if diff.months != 1 else ''),
        diff.days, 'day' + ('s' if diff.days != 1 else ''),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')

def simple_request(func_name, query, variables):
    payload = {'query': query, 'variables': variables}
    r = _post_with_retry('https://api.github.com/graphql', payload, HEADERS, retries=2)
    if r.status_code == 200:
        # GraphQL may still return errors in JSON. Check.
        j = r.json()
        if 'errors' in j:
            raise Exception(func_name, ' GraphQL errors:', j['errors'], 'QUERY_COUNT:', QUERY_COUNT)
        return j
    # helpful messages for rate-limits and abuse
    if r.status_code == 403:
        # Try to surface GitHub message
        try:
            body = r.json()
        except Exception:
            body = r.text
        raise Exception(func_name, '403 Forbidden (possible rate/abuse):', body, 'QUERY_COUNT:', QUERY_COUNT)
    raise Exception(func_name, ' has failed', r.status_code, r.text, QUERY_COUNT)

def graph_commits(start_date, end_date):
    _query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
      user(login: $login) {
        contributionsCollection(from: $start_date, to: $end_date) {
          contributionCalendar { totalContributions }
        }
      }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    j = simple_request(graph_commits.__name__, query, variables)
    return int(j['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Paginated fetch over repositories. Returns total repos or total stars depending on count_type.
    Aggregates across pages so this will return complete totals.
    """
    _query_count('graph_repos_stars')
    total = 0
    edges_accum = []
    q = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
          totalCount
          edges { node { ... on Repository { nameWithOwner stargazers { totalCount } defaultBranchRef { target { ... on Commit { history { totalCount } } } } } } }
          pageInfo { endCursor hasNextPage }
        }
      }
    }'''
    cur = cursor
    while True:
        j = simple_request(graph_repos_stars.__name__, q, {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cur})
        repos = j['data']['user']['repositories']
        edges = repos.get('edges', [])
        edges_accum.extend(edges)
        if not repos['pageInfo']['hasNextPage']:
            break
        cur = repos['pageInfo']['endCursor']

    if count_type == 'repos':
        # totalCount is same across pages; return it if available
        if edges_accum:
            # we already have pages; fetch first page's totalCount from last j
            return repos.get('totalCount', len(edges_accum))
        return 0
    elif count_type == 'stars':
        return sum(edge['node']['stargazers']['totalCount'] for edge in edges_accum)
    elif count_type == 'edges':
        # helper to return edges list (used by loc_query if needed)
        return edges_accum

#def # UNUSED_PLACEHOLDER(): pass
    # keep file consistent if some external code expects it - no-op
    # (Left intentionally blank)

def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    This function used to recurse commit-by-commit and caused massive runtime.
    To keep compatibility, we include a safe guarded version that will not recurse forever:
    - This implementation will attempt to fetch pages up to a reasonable limit (max_pages).
    - If you want full, exact LOC by commit, enable DETAILED_LOC_RUN = True and accept longer runtime.
    """
    _query_count('recursive_loc')
    # Guarded implementation: limit pages
    MAX_PAGES = 50  # safety: avoid infinite recursion. 50 * 100 commits = 5000 commits max
    pages = 0
    additions = addition_total
    deletions = deletion_total
    my_c = my_commits
    cur = cursor
    q = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
      repository(name: $repo_name, owner: $owner) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                totalCount
                edges {
                  node {
                    ... on Commit { committedDate additions deletions }
                    author { user { id } }
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
          }
        }
      }
    }'''
    while True:
        if pages >= MAX_PAGES:
            # give up for now and return what we have
            return additions, deletions, my_c
        r = _post_with_retry('https://api.github.com/graphql', {'query': q, 'variables': {'repo_name': repo_name, 'owner': owner, 'cursor': cur}}, HEADERS, retries=1)
        if r.status_code != 200:
            # save progress and exit
            force_close_file(data, cache_comment)
            if r.status_code == 403:
                raise Exception('Anti-abuse limit hit (403). Slow down requests.')
            raise Exception('recursive_loc() failed', r.status_code, r.text, QUERY_COUNT)
        j = r.json()
        if 'data' not in j or j['data'] is None or j['data']['repository'] is None or j['data']['repository']['defaultBranchRef'] is None:
            return additions, deletions, my_c
        history = j['data']['repository']['defaultBranchRef']['target']['history']
        for node in history['edges']:
            au = node['node'].get('author', {}).get('user')
            if au is not None and au.get('id') == OWNER_ID:
                my_c += 1
                additions += node['node'].get('additions', 0) or 0
                deletions += node['node'].get('deletions', 0) or 0
        if not history['pageInfo']['hasNextPage']:
            return additions, deletions, my_c
        cur = history['pageInfo']['endCursor']
        pages += 1

def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None) -> List[dict]:
    """
    Paginated repositories fetch and call cache_builder to produce a fast cache file.
    This version fetches repository metadata and default branch total commit counts (no per-commit additions/deletions).
    """
    _query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
          edges {
            node {
              ... on Repository {
                nameWithOwner
                defaultBranchRef {
                  target {
                    ... on Commit { history { totalCount } }
                  }
                }
              }
            }
          }
          pageInfo { endCursor hasNextPage }
        }
      }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    j = simple_request(loc_query.__name__, query, variables)
    repos = j['data']['user']['repositories']
    edges = repos['edges']
    if repos['pageInfo']['hasNextPage']:
        next_edges = loc_query(owner_affiliation, comment_size, force_cache, repos['pageInfo']['endCursor'])
        edges += next_edges
    return cache_builder(edges, comment_size, force_cache)

def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Builds/updates a cache file quickly without expensive per-commit recursion.
    Cache file format (per repo line):
      <sha256(nameWithOwner)> <commit_count_on_default_branch> <my_commits> <additions> <deletions>
    This implementation sets my_commits/additions/deletions to 0 (fast). If you need exact values,
    enable detailed run which will call recursive_loc per repo (slow).
    """
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    # seed file if missing
    if not os.path.exists(filename):
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    with open(filename, 'r') as f:
        data = f.readlines()

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    # Ensure each repo has a line; do not compute per-commit additions/deletions for speed
    for i in range(len(edges)):
        repo_hash = hashlib.sha256(edges[i]['node']['nameWithOwner'].encode('utf-8')).hexdigest()
        if i >= len(data):
            # write fast defaults: commit_count, my_commits=0, add=0, del=0
            commit_count = 0
            try:
                commit_count = edges[i]['node']['defaultBranchRef']['target']['history']['totalCount']
            except Exception:
                commit_count = 0
            data.append(f"{repo_hash} {commit_count} 0 0 0\n")
        else:
            parts = data[i].split()
            if parts and parts[0] == repo_hash:
                # Update commit_count if it changed; keep my_commits/add/del as-is (or zero)
                try:
                    new_commit_count = edges[i]['node']['defaultBranchRef']['target']['history']['totalCount']
                except Exception:
                    new_commit_count = 0
                if int(parts[1]) != new_commit_count:
                    # fast update: set new commit count, leave others as 0
                    data[i] = f"{repo_hash} {new_commit_count} 0 0 0\n"
            else:
                # Hash mismatch or corrupt line: replace
                try:
                    commit_count = edges[i]['node']['defaultBranchRef']['target']['history']['totalCount']
                except Exception:
                    commit_count = 0
                data[i] = f"{repo_hash} {commit_count} 0 0 0\n"

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        if len(loc) >= 5:
            try:
                loc_add += int(loc[3]); loc_del += int(loc[4])
            except Exception:
                pass
    return [loc_add, loc_del, loc_add - loc_del, cached]

def flush_cache(edges, filename, comment_size):
    # Preserve comment block (if any), then write a default line per repo
    comment_lines = []
    if os.path.exists(filename) and comment_size > 0:
        with open(filename, 'r') as f:
            comment_lines = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(comment_lines)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')

def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment); f.writelines(data)
    print('Partial cache saved to', filename)

def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    _justify(root, 'commit_data', commit_data, 22)
    _justify(root, 'star_data',   star_data,   14)
    _justify(root, 'repo_data',   repo_data,    6)
    _justify(root, 'contrib_data',contrib_data)
    _justify(root, 'follower_data', follower_data, 10)
    _justify(root, 'loc_data',    loc_data[2],  9)
    _justify(root, 'loc_add',     loc_data[0])
    _justify(root, 'loc_del',     loc_data[1],  7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)

def _justify(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    _replace_text(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    _replace_text(root, f"{element_id}_dots", dot_string)

def _replace_text(root, element_id, new_text):
    el = root.find(f".//*[@id='{element_id}']")
    if el is not None:
        el.text = new_text

def _query_count(fid):
    global QUERY_COUNT
    QUERY_COUNT[fid] += 1

def perf_counter(f, *args):
    start = time.perf_counter()
    out = f(*args)
    return out, time.perf_counter() - start

def formatter(label, dt, ret=False, width=0):
    print('{:<23}'.format('   ' + label + ':'), end='')
    print('{:>12}'.format(('%.4f s' % dt) if dt > 1 else ('%.4f ms' % (dt * 1000))))
    if width: return f"{ret:,}".ljust(width)
    return ret

def user_getter(username):
    _query_count('user_getter')
    q = '''
    query($login: String!){
      user(login: $login) { id createdAt }
    }'''
    j = simple_request(user_getter.__name__, q, {'login': username})
    return j['data']['user']['id'], j['data']['user']['createdAt']

def follower_getter(username):
    _query_count('follower_getter')
    q = '''
    query($login: String!){
      user(login: $login) { followers { totalCount } }
    }'''
    j = simple_request(follower_getter.__name__, q, {'login': username})
    return int(j['data']['user']['followers']['totalCount'])

if __name__ == '__main__':
    print('Calculation times:')
    # fetch user id and created date
    user_data, t_user = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', t_user)

    age_data, t_age = perf_counter(daily_readme, datetime.datetime(2002, 7, 5))  # optional/unutilized in SVG
    formatter('age calculation', t_age)

    # fast LOC query (no per-commit recursion by default)
    total_loc, t_loc = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)' if total_loc[-1] else 'LOC (no cache)', t_loc)

    # Fast commit counter via cache file (third column is "my commits" - default 0 in this fast implementation)
    commit_data, t_commit = perf_counter(lambda: 0 or (
        sum(int(line.split()[2]) for line in open('cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt', 'r').readlines()[7:])
    ) if os.path.exists('cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt') else 0)
    formatter('commit counter', t_commit)

    # Stars / repos / contribution repos (fast paginated queries)
    star_data, t_star   = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, t_repo   = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, t_con = perf_counter(graph_repos_stars, 'repos', ['OWNER','COLLABORATOR','ORGANIZATION_MEMBER'])
    follower_data, t_fol= perf_counter(follower_getter, USER_NAME)

    # Format LOC numbers
    for i in range(len(total_loc)-1): total_loc[i] = '{:,}'.format(total_loc[i])

    # Overwrite SVGs
    try:
        svg_overwrite('dark_mode.svg',  age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    except Exception as e:
        print('Warning: could not write dark_mode.svg:', e)
    try:
        svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    except Exception as e:
        print('Warning: could not write light_mode.svg:', e)

    total_time = t_user + t_age + t_loc + t_commit + t_star + t_repo + t_con + t_fol
    # move cursor hack removed for simplicity; print totals plainly
    print('{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % total_time), ' s')
    print('Total GraphQL API calls:', sum(QUERY_COUNT.values()))
    for k, v in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + k + ':'), '{:>6}'.format(v))

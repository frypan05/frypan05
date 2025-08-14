import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
 
# ---- REQUIRED ENV SECRETS ----
# ACCESS_TOKEN: Fine-grained PAT (read-only is enough; see scopes below)
# USER_NAME: your GitHub username (e.g., "frypan05")
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']  # e.g. 'frypan05'
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'loc_query': 0, 'graph_commits': 0}

# ensure cache dir exists
os.makedirs('cache', exist_ok=True)

def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + ('s' if diff.years != 1 else ''),
        diff.months, 'month' + ('s' if diff.months != 1 else ''),
        diff.days, 'day' + ('s' if diff.days != 1 else ''),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')

def simple_request(func_name, query, variables):
    r = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if r.status_code == 200:
        return r
    raise Exception(func_name, ' failed', r.status_code, r.text, QUERY_COUNT)

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
    r = simple_request(graph_commits.__name__, query, variables)
    return int(r.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    _query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
          totalCount
          edges { node { ... on Repository { nameWithOwner stargazers { totalCount } } } }
          pageInfo { endCursor hasNextPage }
        }
      }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    r = simple_request(graph_repos_stars.__name__, query, variables)
    data = r.json()['data']['user']['repositories']
    if count_type == 'repos':
        return data['totalCount']
    elif count_type == 'stars':
        return sum(edge['node']['stargazers']['totalCount'] for edge in data['edges'])

def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    _query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
      repository(name: $repo_name, owner: $owner) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                totalCount
                edges {
                  node {
                    ... on Commit { committedDate }
                    author { user { id } }
                    deletions
                    additions
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
          }
        }
      }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    r = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if r.status_code == 200:
        default_branch = r.json()['data']['repository']['defaultBranchRef']
        if default_branch is not None:
            history = default_branch['target']['history']
            for node in history['edges']:
                if node['node']['author']['user'] == OWNER_ID:
                    my_commits += 1
                    addition_total += node['node']['additions']
                    deletion_total += node['node']['deletions']
            if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
                return addition_total, deletion_total, my_commits
            else:
                return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])
        return 0
    force_close_file(data, cache_comment)
    if r.status_code == 403:
        raise Exception('Anti-abuse limit hit (403). Slow down requests.')
    raise Exception('recursive_loc() failed', r.status_code, r.text, QUERY_COUNT)

def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
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
    r = simple_request(loc_query.__name__, query, variables)
    repos = r.json()['data']['user']['repositories']
    edges += repos['edges']
    if repos['pageInfo']['hasNextPage']:
        return loc_query(owner_affiliation, comment_size, force_cache, repos['pageInfo']['endCursor'], edges)
    return cache_builder(edges, comment_size, force_cache)

def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    if not os.path.exists(filename):
        # seed file with optional comment block
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
    for i in range(len(edges)):
        repo_hash = hashlib.sha256(edges[i]['node']['nameWithOwner'].encode('utf-8')).hexdigest()
        if i >= len(data):
            data.append(repo_hash + ' 0 0 0 0\n')
        parts = data[i].split()
        if parts and parts[0] == repo_hash:
            try:
                if int(parts[1]) != edges[i]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[i]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[i] = f"{repo_hash} {edges[i]['node']['defaultBranchRef']['target']['history']['totalCount']} {loc[2]} {loc[0]} {loc[1]}\n"
            except TypeError:
                data[i] = f"{repo_hash} 0 0 0 0\n"

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        if len(loc) >= 5:
            loc_add += int(loc[3]); loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]

def flush_cache(edges, filename, comment_size):
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
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
    r = simple_request(user_getter.__name__, q, {'login': username})
    return {'id': r.json()['data']['user']['id']}, r.json()['data']['user']['createdAt']

def follower_getter(username):
    _query_count('follower_getter')
    q = '''
    query($login: String!){
      user(login: $login) { followers { totalCount } }
    }'''
    r = simple_request(follower_getter.__name__, q, {'login': username})
    return int(r.json()['data']['user']['followers']['totalCount'])

if __name__ == '__main__':
    print('Calculation times:')
    user_data, t_user = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', t_user)

    age_data, t_age = perf_counter(daily_readme, datetime.datetime(2002, 7, 5))  # optional/unutilized in SVG
    formatter('age calculation', t_age)

    total_loc, t_loc = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)' if total_loc[-1] else 'LOC (no cache)', t_loc)

    commit_data, t_commit = perf_counter(lambda: 0 or sum(  # fast path via cache file
        int(line.split()[2]) for line in open('cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt', 'r').readlines()[7:]
    ) if os.path.exists('cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt') else 0)
    formatter('commit counter', t_commit)

    star_data, t_star   = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, t_repo   = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, t_con = perf_counter(graph_repos_stars, 'repos', ['OWNER','COLLABORATOR','ORGANIZATION_MEMBER'])
    follower_data, t_fol= perf_counter(follower_getter, USER_NAME)

    for i in range(len(total_loc)-1): total_loc[i] = '{:,}'.format(total_loc[i])

    svg_overwrite('dark_mode.svg',  age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    total_time = t_user + t_age + t_loc + t_commit + t_star + t_repo + t_con + t_fol
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
          '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % total_time), ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')
    print('Total GraphQL API calls:', sum(QUERY_COUNT.values()))
    for k, v in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + k + ':'), '{:>6}'.format(v))

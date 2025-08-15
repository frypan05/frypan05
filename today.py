import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']  # Your GitHub username
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}

# Global variable for owner ID - will be set by user_getter
OWNER_ID = None

# Rate limit handling
RATE_LIMIT_WAIT = 3600  # 1 hour in seconds
MAX_RETRIES = 3

def daily_readme(birthday):
    """Returns the length of time since I was born"""
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')

def format_plural(unit):
    """Returns properly formatted pluralization"""
    return 's' if unit != 1 else ''

def handle_rate_limit(reset_time):
    """Handles rate limiting by waiting until reset"""
    wait_time = max(0, reset_time - time.time())
    if wait_time > 0:
        print(f"Rate limited. Waiting {wait_time:.0f} seconds...")
        time.sleep(wait_time)

def simple_request(func_name, query, variables, retry_count=0):
    """
    Returns a request with rate limit handling and retries
    """
    try:
        request = requests.post('https://api.github.com/graphql', 
                              json={'query': query, 'variables': variables}, 
                              headers=HEADERS)
        
        if request.status_code == 200:
            response_json = request.json()
            if 'errors' in response_json:
                if any(error.get('type') == 'RATE_LIMITED' for error in response_json['errors']):
                    reset_time = int(request.headers.get('X-RateLimit-Reset', time.time() + RATE_LIMIT_WAIT))
                    handle_rate_limit(reset_time)
                    if retry_count < MAX_RETRIES:
                        return simple_request(func_name, query, variables, retry_count + 1)
                    raise Exception(f"Max retries ({MAX_RETRIES}) exceeded for {func_name}")
                raise Exception(f"{func_name} GraphQL errors: {response_json['errors']}")
            return response_json
        
        # Handle other status codes
        if request.status_code == 403 and 'rate limit' in request.text.lower():
            reset_time = int(request.headers.get('X-RateLimit-Reset', time.time() + RATE_LIMIT_WAIT))
            handle_rate_limit(reset_time)
            if retry_count < MAX_RETRIES:
                return simple_request(func_name, query, variables, retry_count + 1)
        
        raise Exception(f"{func_name} failed with status {request.status_code}: {request.text}")
    
    except Exception as e:
        if retry_count < MAX_RETRIES:
            time.sleep(5)  # Wait before retrying
            return simple_request(func_name, query, variables, retry_count + 1)
        raise e

def graph_commits(start_date, end_date):
    """Uses GitHub's GraphQL to return total commit count"""
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """Uses GitHub's GraphQL to return repo/star count"""
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    
    if count_type == 'repos':
        return request['data']['user']['repositories']['totalCount']
    elif count_type == 'stars':
        return stars_counter(request['data']['user']['repositories']['edges'])

def stars_counter(data):
    """Count total stars in repositories"""
    return sum(node['node']['stargazers']['totalCount'] for node in data)

def user_getter(username):
    """Returns the account ID and creation time"""
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return request['data']['user']['id'], request['data']['user']['createdAt']

def follower_getter(username):
    """Returns the number of followers"""
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request['data']['user']['followers']['totalCount'])

def query_count(funct_id):
    """Counts GitHub GraphQL API calls"""
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1

def perf_counter(funct, *args):
    """Times function execution"""
    start = time.perf_counter()
    try:
        funct_return = funct(*args)
        return funct_return, time.perf_counter() - start
    except Exception as e:
        return None, time.perf_counter() - start

def formatter(query_type, difference, funct_return=False, whitespace=0):
    """Formats output with timing information"""
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    time_str = '%.4f' % (difference * 1000) + ' ms' if difference < 1 else '%.4f' % difference + ' s'
    print('{:>12}'.format(time_str))
    if whitespace and funct_return is not False:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return

def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """Updates SVG files with new data"""
    try:
        tree = etree.parse(filename)
        root = tree.getroot()
        justify_format(root, 'commit_data', commit_data, 32)
        justify_format(root, 'star_data', star_data, 35) 
        justify_format(root, 'repo_data', repo_data, 25)
        justify_format(root, 'contrib_data', contrib_data, 15)
        justify_format(root, 'follower_data', follower_data, 33)
        justify_format(root, 'loc_data', loc_data[2], 18)
        justify_format(root, 'loc_add', loc_data[0], 30)
        justify_format(root, 'loc_del', loc_data[1], 30)
        tree.write(filename, encoding='utf-8', xml_declaration=True)
        print(f"Successfully updated {filename}")
    except Exception as e:
        print(f"Error updating {filename}: {e}")

def justify_format(root, element_id, new_text, length=0):
    """Formats text with proper justification"""
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    dot_string = ' ' + ('.' * just_len) + ' ' if just_len > 2 else ['', ' ', '. '][just_len]
    find_and_replace(root, f"{element_id}_dots", dot_string)

def find_and_replace(root, element_id, new_text):
    """Finds and replaces SVG elements"""
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text

def get_total_commits():
    """Gets total commit count"""
    query_count('graph_commits')
    query = '''
    query($login: String!) {
        user(login: $login) {
            contributionsCollection {
                totalCommitContributions
            }
        }
    }'''
    variables = {'login': USER_NAME}
    request = simple_request('get_total_commits', query, variables)
    return request['data']['user']['contributionsCollection']['totalCommitContributions']

def get_basic_loc_estimate():
    """Simple LOC estimation with caching"""
    cache_file = 'cache/loc_estimate.cache'
    try:
        # Try to read from cache first
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                cached_data = f.read().split(',')
                if len(cached_data) == 4:
                    return [int(cached_data[0]), int(cached_data[1]), int(cached_data[2]), True]
    except:
        pass
    
    # Default values if cache fails
    loc_data = [1000, 200, 800, False]
    
    # Save to cache
    try:
        with open(cache_file, 'w') as f:
            f.write(','.join(map(str, loc_data[:-1])))
    except:
        pass
    
    return loc_data

def main():
    """Main execution function"""
    print('Calculation times:')
    
    if 'ACCESS_TOKEN' not in os.environ:
        print("Error: ACCESS_TOKEN environment variable not set")
        exit(1)
    if 'USER_NAME' not in os.environ:
        print("Error: USER_NAME environment variable not set")
        exit(1)
    
    os.makedirs('cache', exist_ok=True)
    
    try:
        # Get user data with rate limit handling
        user_data, user_time = perf_counter(user_getter, USER_NAME)
        if user_data is None:
            raise Exception("Failed to get user data")
        OWNER_ID, acc_date = user_data
        formatter('account data', user_time)
        
        # Calculate age
        age_data, age_time = perf_counter(daily_readme, datetime.datetime(2004, 8, 5))
        formatter('age calculation', age_time)
        
        # Get commit count
        commit_data, commit_time = perf_counter(get_total_commits)
        formatter('commit data', commit_time)
        
        # Get star and repo data
        star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
        formatter('star data', star_time)
        
        repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
        formatter('repo data', repo_time)
        
        contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        formatter('contrib data', contrib_time)
        
        follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
        formatter('follower data', follower_time)
        
        # Get LOC data
        total_loc, loc_time = perf_counter(get_basic_loc_estimate)
        formatter('LOC estimation', loc_time)
        
        # Format LOC numbers
        for index in range(len(total_loc)-1): 
            total_loc[index] = '{:,}'.format(total_loc[index])
        
        # Update SVG files
        svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
        svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
        
        # Print summary
        total_time = user_time + age_time + commit_time + star_time + repo_time + contrib_time + follower_time + loc_time
        print(f'\nTotal function time: {total_time:.4f} s')
        print(f'Total GitHub GraphQL API calls: {sum(QUERY_COUNT.values())}')
        for funct_name, count in QUERY_COUNT.items():
            print(f'   {funct_name}: {count:>6}')
            
        print(f'\nStats Summary:')
        print(f'   Commits: {commit_data:,}')
        print(f'   Stars: {star_data:,}')
        print(f'   Repos (owned): {repo_data:,}')
        print(f'   Repos (contributed): {contrib_data:,}')
        print(f'   Followers: {follower_data:,}')
        
    except Exception as e:
        print(f"Error during execution: {e}")
        exit(1)

if __name__ == '__main__':
    main()

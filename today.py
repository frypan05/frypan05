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


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS)
    if request.status_code == 200:
        response_json = request.json()
        if 'errors' in response_json:
            raise Exception(f"{func_name} GraphQL errors: {response_json['errors']}")
        return response_json
    raise Exception(f"{func_name} failed with status {request.status_code}: {request.text}")


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
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
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
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
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: 
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
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
    """
    Returns the number of followers of the user
    """
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
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
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
    """
    Updates and formats the text of the element, and modifies the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text
    else:
        print(f"Warning: Element with id '{element_id}' not found in SVG")


def get_total_commits():
    """
    Get total commits across all repositories where the user is the author
    """
    query_count('graph_commits')
    
    # Get current year range for contributions
    current_year = datetime.datetime.now().year
    start_date = f"{current_year}-01-01T00:00:00Z"
    end_date = f"{current_year}-12-31T23:59:59Z"
    
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
    """
    Simple LOC estimation - returns placeholder values for now
    For a more accurate count, you'd need to implement the full cache system
    """
    # For now, return placeholder values
    # In a full implementation, this would scan through repositories and count lines
    return [1000, 200, 800, True]  # [additions, deletions, total, cached]


if __name__ == '__main__':
    """
    Main execution
    """
    print('Calculation times:')
    
    # Check if required environment variables are set
    if 'ACCESS_TOKEN' not in os.environ:
        print("Error: ACCESS_TOKEN environment variable not set")
        exit(1)
    if 'USER_NAME' not in os.environ:
        print("Error: USER_NAME environment variable not set")
        exit(1)
    
    # Create cache directory if it doesn't exist
    os.makedirs('cache', exist_ok=True)
    
    try:
        # Define global variable for owner ID and calculate user's creation date
        user_data, user_time = perf_counter(user_getter, USER_NAME)
        OWNER_ID, acc_date = user_data
        formatter('account data', user_time)
        
        # Calculate age (you can modify the birthday date)
        age_data, age_time = perf_counter(daily_readme, datetime.datetime(2000, 1, 1))  # Change this to your birthday
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
        
        # Get LOC data (simplified version)
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
        import traceback
        traceback.print_exc()
        exit(1)

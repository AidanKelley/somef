# creatJSON.py
# parameters:
## input file: either: url to github repository OR markdown documentation file path
## output file: json with each excerpt marked with all four classification scores

import argparse
import json
import base64
from urllib.parse import urlparse
import sys
import os
from os import path
from pathlib import Path
import requests
from markdown import Markdown
from bs4 import BeautifulSoup
from io import StringIO
import pickle
import pprint
import pandas as pd
import numpy as np
import re
from .configuration import get_config

from somef.data_to_graph import DataGraph

from . import createExcerpts
from . import header_analysis


## Markdown to plain text conversion: begin ##
# code snippet from https://stackoverflow.com/a/54923798
def unmark_element(element, stream=None):
    if stream is None:
        stream = StringIO()
    if element.text:
        stream.write(element.text)
    for sub in element:
        unmark_element(sub, stream)
    if element.tail:
        stream.write(element.tail)
    return stream.getvalue()


# patching Markdown
Markdown.output_formats["plain"] = unmark_element
__md = Markdown(output_format="plain")
__md.stripTopLevelTags = False


def unmark(text):
    return __md.convert(text)


## Markdown to plain text conversion: end ##

def restricted_float(x):
    x = float(x)
    if x < 0.0 or x > 1.0:
        raise argparse.ArgumentTypeError(f"{x} not in range [0.0, 1.0]")
    return x


categories = ['description', 'citation', 'installation', 'invocation']
# keep_keys = ('description', 'name', 'owner', 'license', 'languages_url', 'forks_url')
# instead of keep keys, we have this table
# it says that we want the key "codeRepository", and that we'll get it from the path "html_url" within the result object
github_crosswalk_table = {
    "codeRepository": "html_url",
    "languages_url": "languages_url",
    "downloadUrl": "archive_url",  # todo: I think I got this from CodeMeta but it seems wrong
    "owner": ["owner", "login"],
    "ownerType": ["owner", "type"],  # used to determine if owner is User or Organization
    "dateCreated": "created_at",
    "dateModified": "updated_at",
    "license": "license",
    "description": "description",
    "name": "name",
    "fullName": "name",
    "issueTracker": "issues_url",
    "forks_url": "forks_url"
}

release_crosswalk_table = {
    'tag_name': 'tag_name',
    'name': 'name',
    'author_name': ['author', 'login'],
    'authorType': ['author', 'type'],
    'body': 'body',
    'tarball_url': 'tarball_url',
    'zipball_url': 'zipball_url',
    'html_url': 'html_url',
    'url': 'url',
    'dateCreated': 'created_at',
    'datePublished': "published_at",
}


## Function uses the repository_url provided to load required information from github.
## Information kept from the repository is written in keep_keys.
## Returns the readme text and required metadata
def load_repository_metadata(repository_url, header):
    print("Loading Repository Information....")
    ## load general response of the repository
    if repository_url[-1] == '/':
        repository_url = repository_url[:-1]
    url = urlparse(repository_url)
    if url.netloc != 'github.com':
        sys.exit("Error: repository must come from github")
    if len(url.path.split('/')) != 3:
        sys.exit("Github link is not correct. \nThe correct format is https://github.com/owner/repo_name.")
    _, owner, repo_name = url.path.split('/')
    general_resp = requests.get(f"https://api.github.com/repos/{owner}/{repo_name}", headers=header).json()

    if 'message' in general_resp.keys() and general_resp['message'] == "Not Found":
        sys.exit("Error: repository name is incorrect")

    if 'message' in general_resp.keys():
        message = general_resp['message']
        sys.exit("Error: " + message)

    ## get only the fields that we want
    def do_crosswalk(data, crosswalk_table):
        def get_path(obj, path):
            if isinstance(path, list) or isinstance(path, tuple):
                if len(path) == 1:
                    path = path[0]
                else:
                    return get_path(obj[path[0]], path[1:])

            return obj[path] if path in obj else None

        output = {}
        for codemeta_key, path in crosswalk_table.items():
            value = get_path(data, path)
            if value is not None:
                output[codemeta_key] = value
            else:
                print(f"Error: key {path} not present in github repository")
        return output

    filtered_resp = do_crosswalk(general_resp, github_crosswalk_table)

    ## condense license information
    license_info = {}
    for k in ('name', 'url'):
        if 'license' in filtered_resp and k in filtered_resp['license']:
            license_info[k] = filtered_resp['license'][k]
    filtered_resp['license'] = license_info

    # get keywords / topics
    topics_headers = header
    topics_headers['accept'] = 'application/vnd.github.mercy-preview+json'
    topics_resp = requests.get('https://api.github.com/repos/' + owner + "/" + repo_name + '/topics',
                               headers=topics_headers).json()
    if 'message' in topics_resp.keys():
        sys.exit("Error: " + topics_resp['message'])
    if topics_resp and 'names' in topics_resp.keys():
        filtered_resp['topics'] = topics_resp['names']

    ## get languages
    filtered_resp['languages'] = list(requests.get(filtered_resp['languages_url']).json().keys())
    del filtered_resp['languages_url']

    ## get default README
    readme_info = requests.get('https://api.github.com/repos/' + owner + "/" + repo_name + '/readme',
                               headers=topics_headers).json()
    if 'message' in readme_info.keys():
        sys.exit("Error: " + general_resp['message'])
    readme = base64.b64decode(readme_info['content']).decode("utf-8")
    text = readme
    filtered_resp['readme_url'] = readme_info['html_url']

    ## get releases
    releases_list = requests.get('https://api.github.com/repos/' + owner + "/" + repo_name + '/releases',
                                 headers=header).json()

    if isinstance(releases_list, dict) and 'message' in releases_list.keys():
        sys.exit("Error: " + general_resp['message'])
    releases_list = [do_crosswalk(release, release_crosswalk_table) for release in releases_list]
    filtered_resp['releases'] = list(releases_list)

    print("Repository Information Successfully Loaded. \n")
    return text, filtered_resp


## Function takes readme text as input and divides it into excerpts
## Returns the extracted excerpts
def create_excerpts(string_list):
    print("Splitting text into valid excerpts for classification")
    divisions = createExcerpts.split_into_excerpts(string_list)
    print("Text Successfully split. \n")
    return divisions


## Function takes readme text as input and runs the provided classifiers on it
## Returns the dictionary containing scores for each excerpt.
def run_classifiers(excerpts, file_paths):
    score_dict = {}
    for category in categories:
        if category not in file_paths.keys():
            sys.exit("Error: Category " + category + " file path not present in config.json")
        file_name = file_paths[category]
        if not path.exists(file_name):
            sys.exit(f"Error: File/Directory {file_name} does not exist")
        print("Classifying excerpts for the catgory", category)
        classifier = pickle.load(open(file_name, 'rb'))
        scores = classifier.predict_proba(excerpts)
        score_dict[category] = {'excerpt': excerpts, 'confidence': scores[:, 1]}
        print("Excerpt Classification Successful for the Category", category)
    print("\n")
    return score_dict


## Function removes all excerpt lines which have been classified but contain only one word.
## Returns the excerpt to be entered into the predictions
def remove_unimportant_excerpts(excerpt_element):
    excerpt_info = excerpt_element['excerpt']
    excerpt_confidence = excerpt_element['confidence']
    excerpt_lines = excerpt_info.split('\n')
    final_excerpt = {'excerpt': "", 'confidence': [], 'technique': 'classifier'}
    for i in range(len(excerpt_lines) - 1):
        words = excerpt_lines[i].split(' ')
        if len(words) == 2:
            continue
        final_excerpt['excerpt'] += excerpt_lines[i] + '\n';
        final_excerpt['confidence'].append(excerpt_confidence[i])
    return final_excerpt


## Function takes scores dictionary and a threshold as input
## Returns predictions containing excerpts with a confidence above the given threshold.
def classify(scores, threshold):
    print("Checking Thresholds for Classified Excerpts.")
    predictions = {}
    for ele in scores.keys():
        print("Running for", ele)
        flag = False
        predictions[ele] = []
        excerpt = ""
        confid = []
        for i in range(len(scores[ele]['confidence'])):
            if scores[ele]['confidence'][i] >= threshold:
                if flag == False:
                    excerpt = excerpt + scores[ele]['excerpt'][i] + ' \n'
                    confid.append(scores[ele]['confidence'][i])
                    flag = True
                else:
                    excerpt = excerpt + scores[ele]['excerpt'][i] + ' \n'
                    confid.append(scores[ele]['confidence'][i])
            else:
                if flag == True:
                    element = remove_unimportant_excerpts({'excerpt': excerpt, 'confidence': confid})
                    if len(element['confidence']) != 0:
                        predictions[ele].append(element)
                    excerpt = ""
                    confid = []
                    flag = False
        print("Run completed.")
    print("All Excerpts below the given Threshold Removed. \n")
    return predictions


## Function adds category information extracted using header information
## Returns json with the information added.
def extract_categories_using_header(repo_data):
    print("Extracting information using headers")
    header_info, string_list = header_analysis.extract_categories_using_headers(repo_data)
    print("Information extracted. \n")
    return header_info, string_list


## Function takes readme text as input and runs a regex parser on it
## Returns a list of bibtex citations
def extract_bibtex(readme_text):
    print("Extracting bibtex citation from readme")
    regex = r'\@[a-zA-z]+\{[.\n\S\s]+?[author|title][.\n\S\s]+?[author|title][.\n\S\s]+?\n\}'
    excerpts = readme_text
    citations = re.findall(regex, excerpts)
    print("Extracting bibtex citation from readme completed. \n")
    return citations


## Function takes the predictions using header information, classifier and bibtek parser
## Returns a combined predictions
def merge(header_predictions, predictions, citations):
    print("Merge prediction using header information, classifier and bibtek parser")
    for i in range(len(citations)):
        if 'citation' not in predictions.keys():
            predictions['citation'] = []
        predictions['citation'].insert(0, {'excerpt': citations[i], 'confidence': [1.0], 'technique': 'classifier'})

    for headers in header_predictions:
        if headers not in predictions.keys():
            predictions[headers] = header_predictions[headers]
        else:
            for h in header_predictions[headers]:
                predictions[headers].insert(0, h)
    print("Merging successful. \n")
    return predictions


## Function takes metadata, readme text predictions, bibtex citations and path to the output file
## Performs some combinations
def format_output(git_data, repo_data):
    for i in git_data.keys():
        if i == 'description':
            if 'description' not in repo_data.keys():
                repo_data['description'] = []
            repo_data['description'].append({'excerpt': git_data[i], 'confidence': [1.0], 'technique': 'metadata'})
        else:
            repo_data[i] = {'excerpt': git_data[i], 'confidence': [1.0], 'technique': 'metadata'}

    return repo_data


# saves the final json Object in the file
def save_json_output(repo_data, outfile):
    print("Saving json data to", outfile)
    with open(outfile, 'w') as output:
        json.dump(repo_data, output)

    ## Function takes metadata, readme text predictions, bibtex citations and path to the output file


## Performs some combinations and saves the final json Object in the file
def save_json(git_data, repo_data, outfile):
    repo_data = format_output(git_data, repo_data)
    save_json_output(repo_data, outfile)


def cli_get_data(threshold, repo_url=None, doc_src=None):
    file_paths = get_config()
    header = {}
    if 'Authorization' in file_paths.keys():
        header['Authorization'] = file_paths['Authorization']
    header['accept'] = 'application/vnd.github.v3+json'
    if repo_url is not None:
        assert (doc_src is None)
        text, github_data = load_repository_metadata(repo_url, header)
    else:
        assert (doc_src is not None)
        if not path.exists(doc_src):
            sys.exit("Error: Document does not exist at given path")
        with open(doc_src, 'r') as doc_fh:
            text = doc_fh.read()
        github_data = {}

    unfiltered_text = text
    header_predictions, string_list = extract_categories_using_header(unfiltered_text)
    text = unmark(text)
    excerpts = create_excerpts(string_list)
    score_dict = run_classifiers(excerpts, file_paths)
    predictions = classify(score_dict, threshold)
    citations = extract_bibtex(text)
    predictions = merge(header_predictions, predictions, citations)
    return format_output(github_data, predictions)


def get_zenodo_data(query):
    config = get_config()
    if not ("zenodo_auth" in config and len(config["zenodo_auth"]) > 0):
        exit("must supply a zenodo authentication toke using somef_configure")
    zenodo_api_base = 'https://zenodo.org/api'
    zenodo_access_token = config["zenodo_auth"]
    query_size = 10
    index = 0
    # initialize it big enough to run once
    total_count = query_size + 1

    output_results = {}

    while query_size * index < total_count:
        response = requests.get(
            f"{zenodo_api_base}/records",
            params={
                'q': query,
                'size': query_size,
                'type': 'software',
                'page': index + 1,
                'access_token': zenodo_access_token
            }
        )
        data_out = response.json()
        print(data_out["links"])

        results = data_out["hits"]["hits"]
        total_count = data_out["hits"]["total"]

        def get_github_url(result):
            try:
                metadata = result['metadata']
                assert('related_identifiers' in metadata)
                related_identifiers = metadata['related_identifiers']
                for identifier in related_identifiers:
                    if identifier['relation'] == 'isSupplementTo':
                        github_base_url = 'https://github.com/'
                        github_url = identifier['identifier']
                        assert(github_base_url in github_url)
                        # now, process the URL
                        _, _, path = github_url.partition(github_base_url)
                        path_components = path.split('/', 2)

                        return github_base_url + "/".join(path_components[:2])
            except AssertionError:
                pass

            return None

        processed_results = ((result, get_github_url(result)) for result in results)
        output_results.update({result["id"]: {"github_url": github_url, "zenodo_data": result}
                               for result, github_url in processed_results if github_url is not None})
        index += 1

    with open("test_zenodo_results.json", "w") as test_out:
        json.dump(output_results, test_out)

    return output_results

# Function runs all the required components of the cli on a given document file
def run_cli_document(doc_src, threshold, output):
    return run_cli(threshold=threshold, output=output, doc_src=doc_src)


# Function runs all the required components of the cli for a repository
def run_cli(*,
            threshold=0.8,
            repo_url=None,
            doc_src=None,
            in_file=None,
            zenodo_queries=None,
            output=None,
            graph_out=None,
            graph_format="turtle",
            ):
    multiple_repos = in_file is not None or zenodo_queries is not None
    if in_file is not None:
        with open(in_file, "r") as in_handle:
            # get the line (with the final newline omitted) if the line is not empty
            repo_set = {line[:-1] for line in in_handle if len(line) > 1}

        # convert to a set to ensure uniqueness (we don't want to get the same data multiple times)
        repo_data = [cli_get_data(threshold, repo_url=repo_url) for repo_url in repo_set]
    elif zenodo_queries is not None:
        with open(zenodo_queries, "r") as in_handle:
            # get all of the queries
            queries = {line[:-1] for line in in_handle if len(line) > 1}
            # get the data from zenodo for each
            data_and_urls = (get_zenodo_data(query) for query in queries)
            # flatten it all into one object and use a dict to guarantee uniqueness
            data_and_urls_flattened = {key: value for data_out in data_and_urls
                                       for key, value in data_out.items()}

            # make sure that the github urls are all unique, too
            github_urls = {data["github_url"] for data in data_and_urls_flattened.values()}
            # get the data from the cli
            cli_data = {github_url: cli_get_data(threshold, repo_url=github_url) for github_url in github_urls}

            # create the data object, with original data and zenodo data added
            repo_data = [{
                             **cli_data[data["github_url"]],
                             "zenodo_data": [{
                                 "excerpt": data["zenodo_data"],
                                 "confidence": 1,
                                 "technique": "metadata"
                             }]
                         }
                         for key, data in data_and_urls_flattened.items()]
    else:
        if repo_url:
            repo_data = cli_get_data(threshold, repo_url=repo_url)
        else:
            repo_data = cli_get_data(threshold, doc_src=doc_src)

    if output is not None:
        save_json_output(repo_data, output)

    if graph_out is not None:
        print("Generating Knowledge Graph")
        data_graph = DataGraph()
        if multiple_repos:
            for repo in repo_data:
                data_graph.add_somef_data(repo)
        else:
            data_graph.add_somef_data(repo_data)

        print("Saving Knowledge Graph ttl data to", graph_out)
        with open(graph_out, "wb") as out_file:
            out_file.write(data_graph.g.serialize(format=graph_format))

import os
from pathlib import Path
import json

__DEFAULT_SOMEF_CONFIGURATION_FILE__ = "~/.somef/config.json"

path = Path(__file__).parent.absolute()
default_description = str(path)+"/models/description.sk"
default_invocation = str(path)+"/models/invocation.sk"
default_installation = str(path)+"/models/installation.sk"
default_citation = str(path)+"/models/citation.sk"

current_config = None


def configure(
        authorization="",
        zenodo_auth="",
        description=default_description,
        invocation=default_invocation,
        installation=default_installation,
        citation=default_citation):

    credentials_file = Path(
        os.getenv("SOMEF_CONFIGURATION_FILE", __DEFAULT_SOMEF_CONFIGURATION_FILE__)
    ).expanduser()
    os.makedirs(str(credentials_file.parent), exist_ok=True)

    # credentials_file = Path(os.getenv("SOMEF_CONFIGURATION_FILE", __DEFAULT_SOMEF_CONFIGURATION_FILE__)).expanduser()

    if credentials_file.exists():
        with credentials_file.open("r") as fh:
            data = json.load(fh)
    else:
        data = {}

    # don't overwrite these if nothing is passed in
    if not authorization == "":
        data['Authorization'] = f"token {authorization}"
    if not zenodo_auth == "":
        data["zenodo_auth"] = zenodo_auth
    if not description == "":
        data["description"] = description
    if not invocation == "":
        data["invocation"] = invocation
    if not installation == "":
        data["installation"] = installation
    if not citation == "":
        data["citation"] = citation

    with credentials_file.open("w") as fh:
        credentials_file.parent.chmod(0o700)
        credentials_file.chmod(0o600)
        json.dump(data, fh) 

    global current_config
    current_config = data


def get_config():
    global current_config
    if current_config is None:
        credentials_file = Path(
            os.getenv("SOMEF_CONFIGURATION_FILE", __DEFAULT_SOMEF_CONFIGURATION_FILE__)
        ).expanduser()
        if not credentials_file.exists():
            return None
        with credentials_file.open("r") as fh:
            current_config = json.load(fh)
        if not ("description" in current_config
                and "invocation" in current_config
                and "installation" in current_config
                and "citation" in current_config):
            return None

    return current_config

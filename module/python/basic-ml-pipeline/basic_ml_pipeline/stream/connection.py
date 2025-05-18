import requests
from basic_ml_pipeline.config import CONFIG


def open_stream() -> requests.Response:
    endpoint = f"{CONFIG.x_api_url}/tweets"
    headers = {"Authorization": f"Bearer {CONFIG.x_api_bearer_token}"}
    response = requests.request("GET", endpoint, headers=headers)
    return response
import os

from aispace.github import GitHubClient
from aispace.main import create_app

app = create_app(
    github_client_factory=lambda store: GitHubClient(
        store, base_url=os.environ["AISPACE_GITHUB_MOCK_URL"]
    )
)

#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "lipi" / "lipi-checks-dashboard.html"
GRAPHQL_URL = "https://api.github.com/graphql"
ISSUE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/issues/(\d+)$")
DISCUSSION_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/discussions/(\d+)$")


def require_token() -> str:
	token = os.environ.get("GITHUB_TOKEN", "").strip()
	if not token:
		raise SystemExit("GITHUB_TOKEN is required to refresh dashboard metadata")
	return token


def fetch_graphql(token: str, query: str) -> dict:
	payload = json.dumps({"query": query}).encode("utf-8")
	request = Request(
		GRAPHQL_URL,
		data=payload,
		headers={
			"Authorization": f"Bearer {token}",
			"Content-Type": "application/json",
			"Accept": "application/vnd.github+json",
			"User-Agent": "lipi-checks-dashboard-refresh",
		},
		method="POST",
	)
	try:
		with urlopen(request) as response:
			body = json.loads(response.read().decode("utf-8"))
	except HTTPError as error:
		raise SystemExit(f"GitHub GraphQL request failed: HTTP {error.code}") from error

	if body.get("errors"):
		raise SystemExit(f"GitHub GraphQL returned errors: {body['errors']}")
	return body["data"]


def extract_embedded_json(html: str, name: str, next_name: str) -> tuple[str, object]:
	pattern = re.compile(rf"const {name} = (.*?);\n\s*const {next_name} = ", re.S)
	match = pattern.search(html)
	if not match:
		raise SystemExit(f"Unable to locate const {name} in dashboard HTML")
	return match.group(1), json.loads(match.group(1))


def batched(items: list[tuple], size: int) -> list[list[tuple]]:
	return [items[index:index + size] for index in range(0, len(items), size)]


def build_issue_type_map(rows: list[dict], token: str) -> dict[str, str]:
	issue_refs: list[tuple[str, str, str, int]] = []
	discussion_refs: list[tuple[str, str, str, int]] = []

	for row in rows:
		link = (row.get("Link") or "").strip()
		issue_match = ISSUE_RE.match(link)
		discussion_match = DISCUSSION_RE.match(link)
		if issue_match:
			owner, repo, number = issue_match.groups()
			issue_refs.append((link, owner, repo, int(number)))
		elif discussion_match:
			owner, repo, number = discussion_match.groups()
			discussion_refs.append((link, owner, repo, int(number)))

	issue_refs = list(dict.fromkeys(issue_refs))
	discussion_refs = list(dict.fromkeys(discussion_refs))

	refreshed: dict[str, str] = {}

	for batch in batched(issue_refs, 20):
		fields = []
		for index, (_, owner, repo, number) in enumerate(batch):
			fields.append(
				f'i{index}: repository(owner:"{owner}", name:"{repo}") {{ issue(number:{number}) {{ issueType {{ name }} }} }}'
			)
		data = fetch_graphql(token, "query { " + " ".join(fields) + " }")
		for index, (link, _, _, _) in enumerate(batch):
			issue = ((data.get(f"i{index}") or {}).get("issue") or {})
			refreshed[link] = (((issue.get("issueType") or {}).get("name")) or "—")

	for batch in batched(discussion_refs, 20):
		fields = []
		for index, (_, owner, repo, number) in enumerate(batch):
			fields.append(
				f'd{index}: repository(owner:"{owner}", name:"{repo}") {{ discussion(number:{number}) {{ category {{ name }} }} }}'
			)
		data = fetch_graphql(token, "query { " + " ".join(fields) + " }")
		for index, (link, _, _, _) in enumerate(batch):
			discussion = ((data.get(f"d{index}") or {}).get("discussion") or {})
			refreshed[link] = (((discussion.get("category") or {}).get("name")) or "Discussion")

	return dict(sorted(refreshed.items()))


def refresh_generated_meta(html: str) -> str:
	timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
	pattern = re.compile(r"Generated:\s*[^|<]+\|\s*Source:\s*[^<]+")
	replacement = f"Generated: {timestamp} | Source: embedded inventory + GitHub metadata"
	if not pattern.search(html):
		raise SystemExit("Unable to locate generated metadata line in dashboard HTML")
	return pattern.sub(replacement, html, count=1)


def replace_issue_type_map(html: str, issue_type_map: dict[str, str]) -> str:
	serialized = json.dumps(issue_type_map, ensure_ascii=False, indent=6)
	pattern = re.compile(r"const issueTypeByLink = \{.*?\};\n\s*const tbodyMain = ", re.S)
	replacement = f"const issueTypeByLink = {serialized};\n    const tbodyMain = "
	if not pattern.search(html):
		raise SystemExit("Unable to locate issueTypeByLink in dashboard HTML")
	return pattern.sub(replacement, html, count=1)


def main() -> int:
	token = require_token()
	html = DASHBOARD_PATH.read_text(encoding="utf-8")
	_, rows = extract_embedded_json(html, "data", "issueTypeByLink")
	refreshed_map = build_issue_type_map(rows, token)
	html = replace_issue_type_map(html, refreshed_map)
	html = refresh_generated_meta(html)
	DASHBOARD_PATH.write_text(html, encoding="utf-8")
	print(f"Refreshed {len(refreshed_map)} GitHub metadata links in {DASHBOARD_PATH.name}")
	return 0


if __name__ == "__main__":
	sys.exit(main())

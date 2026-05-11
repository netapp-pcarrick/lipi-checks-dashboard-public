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


def extract_embedded_json(html: str, name: str, next_name: str) -> object:
	pattern = re.compile(rf"const {name} = (.*?);\n\s*const {next_name} = ", re.S)
	match = pattern.search(html)
	if not match:
		raise SystemExit(f"Unable to locate const {name} in dashboard HTML")
	return json.loads(match.group(1))


def batched(items: list[tuple], size: int) -> list[list[tuple]]:
	return [items[index:index + size] for index in range(0, len(items), size)]


def collect_refs(rows: list[dict]) -> tuple[list[tuple[str, str, str, int]], list[tuple[str, str, str, int]]]:
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

	return list(dict.fromkeys(issue_refs)), list(dict.fromkeys(discussion_refs))


def normalize_date(value: str | None) -> str:
	if not value:
		return "—"
	return value[:10]


def fetch_issue_details(issue_refs: list[tuple[str, str, str, int]], token: str) -> dict[str, dict[str, str]]:
	refreshed: dict[str, dict[str, str]] = {}

	for batch in batched(issue_refs, 20):
		fields = []
		for index, (_, owner, repo, number) in enumerate(batch):
			fields.append(
				f'i{index}: repository(owner:"{owner}", name:"{repo}") {{ issue(number:{number}) {{ number createdAt closedAt author {{ login }} labels(first: 50) {{ nodes {{ name }} }} issueType {{ name }} }} }}'
			)
		data = fetch_graphql(token, "query { " + " ".join(fields) + " }")
		for index, (link, _, _, _) in enumerate(batch):
			issue = ((data.get(f"i{index}") or {}).get("issue") or {})
			labels = "; ".join(
				node.get("name", "")
				for node in (issue.get("labels") or {}).get("nodes", [])
				if node.get("name")
			) or "—"
			refreshed[link] = {
				"number": str(issue.get("number") or ""),
				"created": normalize_date(issue.get("createdAt")),
				"closed": normalize_date(issue.get("closedAt")),
				"author": ((issue.get("author") or {}).get("login")) or "—",
				"labels": labels,
				"issueType": (((issue.get("issueType") or {}).get("name")) or "—"),
			}

	return refreshed


def fetch_discussion_details(discussion_refs: list[tuple[str, str, str, int]], token: str) -> dict[str, dict[str, str]]:
	refreshed: dict[str, dict[str, str]] = {}

	for batch in batched(discussion_refs, 20):
		fields = []
		for index, (_, owner, repo, number) in enumerate(batch):
			fields.append(
				f'd{index}: repository(owner:"{owner}", name:"{repo}") {{ discussion(number:{number}) {{ number createdAt closedAt category {{ name }} author {{ login }} }} }}'
			)
		data = fetch_graphql(token, "query { " + " ".join(fields) + " }")
		for index, (link, _, _, _) in enumerate(batch):
			discussion = ((data.get(f"d{index}") or {}).get("discussion") or {})
			refreshed[link] = {
				"number": str(discussion.get("number") or ""),
				"created": normalize_date(discussion.get("createdAt")),
				"closed": normalize_date(discussion.get("closedAt")),
				"author": ((discussion.get("author") or {}).get("login")) or "—",
				"issueType": (((discussion.get("category") or {}).get("name")) or "Discussion"),
			}

	return refreshed


def build_issue_type_map(
	issue_details: dict[str, dict[str, str]],
	discussion_details: dict[str, dict[str, str]],
) -> dict[str, str]:
	refreshed: dict[str, str] = {}
	for link, details in issue_details.items():
		refreshed[link] = details.get("issueType", "—") or "—"
	for link, details in discussion_details.items():
		refreshed[link] = details.get("issueType", "Discussion") or "Discussion"
	return dict(sorted(refreshed.items()))


def refresh_rows(
	rows: list[dict],
	issue_details: dict[str, dict[str, str]],
	discussion_details: dict[str, dict[str, str]],
) -> list[dict]:
	refreshed_rows: list[dict] = []

	for row in rows:
		updated = dict(row)
		link = (row.get("Link") or "").strip()

		if link in issue_details:
			details = issue_details[link]
			updated["Issue/Disc #"] = details.get("number") or updated.get("Issue/Disc #") or "—"
			updated["Labels"] = details.get("labels") or "—"
			updated["Author"] = details.get("author") or "—"
			updated["Created"] = details.get("created") or "—"
			updated["Closed"] = details.get("closed") or "—"
		elif link in discussion_details:
			details = discussion_details[link]
			updated["Issue/Disc #"] = details.get("number") or updated.get("Issue/Disc #") or "—"
			updated["Author"] = details.get("author") or "—"
			updated["Created"] = details.get("created") or "—"
			updated["Closed"] = details.get("closed") or "—"

		refreshed_rows.append(updated)

	return refreshed_rows


def refresh_generated_meta(html: str) -> str:
	timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
	pattern = re.compile(r"Generated:\s*[^|<]+\|\s*Source:\s*[^<]+")
	replacement = f"Generated: {timestamp} | Source: embedded inventory + GitHub GraphQL metadata"
	if not pattern.search(html):
		raise SystemExit("Unable to locate generated metadata line in dashboard HTML")
	return pattern.sub(replacement, html, count=1)


def replace_embedded_data(html: str, rows: list[dict]) -> str:
	serialized = json.dumps(rows, ensure_ascii=False)
	pattern = re.compile(r"const data = \[.*?\];\n\s*const issueTypeByLink = ", re.S)
	replacement = f"const data = {serialized};\n    const issueTypeByLink = "
	if not pattern.search(html):
		raise SystemExit("Unable to locate embedded data in dashboard HTML")
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
	rows = extract_embedded_json(html, "data", "issueTypeByLink")
	issue_refs, discussion_refs = collect_refs(rows)
	issue_details = fetch_issue_details(issue_refs, token)
	discussion_details = fetch_discussion_details(discussion_refs, token)
	rows = refresh_rows(rows, issue_details, discussion_details)
	refreshed_map = build_issue_type_map(issue_details, discussion_details)
	html = replace_embedded_data(html, rows)
	html = replace_issue_type_map(html, refreshed_map)
	html = refresh_generated_meta(html)
	DASHBOARD_PATH.write_text(html, encoding="utf-8")
	print(f"Refreshed {len(rows)} rows and {len(refreshed_map)} GitHub metadata links in {DASHBOARD_PATH.name}")
	return 0


if __name__ == "__main__":
	sys.exit(main())

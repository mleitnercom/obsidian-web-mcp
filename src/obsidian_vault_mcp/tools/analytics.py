"""Vault analytics tools for hygiene and structural diagnostics."""

import re
from collections import Counter, defaultdict
from pathlib import Path

import frontmatter

from .. import config
from ..vault import resolve_vault_path, scan_markdown_encoding_issues, vault_json_dumps

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _iter_markdown_files(path_prefix: str = "") -> tuple[Path, list[Path]]:
    root = resolve_vault_path(path_prefix) if path_prefix else config.VAULT_PATH.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {path_prefix}")

    files: list[Path] = []
    for path in root.rglob("*.md"):
        if any(part in config.EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path)
    return root, files


def _load_posts(path_prefix: str = "") -> tuple[list[dict], dict[str, list[str]]]:
    vault_root = config.VAULT_PATH.resolve()
    _, files = _iter_markdown_files(path_prefix)
    posts: list[dict] = []
    basename_index: dict[str, list[str]] = defaultdict(list)

    for path in files:
        rel = str(path.relative_to(vault_root)).replace("\\", "/")
        basename_index[path.stem.lower()].append(rel)
        try:
            raw = path.read_text(encoding="utf-8")
            post = frontmatter.loads(raw)
            metadata = dict(post.metadata)
        except UnicodeDecodeError:
            raw = ""
            metadata = {}
        except Exception:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            metadata = {}
        posts.append(
            {
                "path": rel,
                "text": raw,
                "frontmatter": metadata,
                "name": path.stem,
            }
        )

    return posts, basename_index


def _extract_tags(frontmatter_data: dict) -> list[str]:
    tags = frontmatter_data.get("tags", [])
    if isinstance(tags, str):
        return [tags]
    if isinstance(tags, list):
        return [str(tag) for tag in tags]
    return []


def _resolve_wikilink_target(target: str, basename_index: dict[str, list[str]]) -> bool:
    clean = target.split("|", 1)[0].split("#", 1)[0].strip()
    if not clean:
        return True
    candidate = clean.replace("\\", "/")
    if "/" in candidate or "." in Path(candidate).name:
        candidate_path = candidate if Path(candidate).suffix else f"{candidate}.md"
        resolved = config.VAULT_PATH.resolve() / candidate_path
        return resolved.exists()
    return bool(basename_index.get(candidate.lower()))


def _frontmatter_missing(posts: list[dict]) -> list[dict]:
    return [{"path": post["path"]} for post in posts if not post["frontmatter"]]


def _required_frontmatter_missing(posts: list[dict], required_fields: list[str]) -> list[dict]:
    if not required_fields:
        return []
    findings = []
    for post in posts:
        missing = [field for field in required_fields if field not in post["frontmatter"]]
        if missing:
            findings.append({"path": post["path"], "missing_fields": missing})
    return findings


def _broken_wikilinks(posts: list[dict], basename_index: dict[str, list[str]]) -> list[dict]:
    findings = []
    for post in posts:
        for match in WIKILINK_RE.findall(post["text"]):
            if not _resolve_wikilink_target(match, basename_index):
                findings.append({"path": post["path"], "target": match})
    return findings


def _suspicious_tag_variants(posts: list[dict]) -> list[dict]:
    raw_by_normalized: dict[str, set[str]] = defaultdict(set)
    usage_count: Counter[str] = Counter()
    for post in posts:
        for tag in _extract_tags(post["frontmatter"]):
            normalized = tag.strip().lower()
            if not normalized:
                continue
            raw_by_normalized[normalized].add(tag)
            usage_count[normalized] += 1

    findings = []
    for normalized, variants in raw_by_normalized.items():
        if len(variants) > 1:
            findings.append(
                {
                    "normalized_tag": normalized,
                    "variants": sorted(variants),
                    "usage_count": usage_count[normalized],
                }
            )
    return sorted(findings, key=lambda item: (-item["usage_count"], item["normalized_tag"]))


def vault_analytics_summary(
    path_prefix: str = "",
    required_frontmatter: list[str] | None = None,
    max_examples: int = 3,
) -> str:
    """Return a compact analytics summary for vault hygiene."""
    try:
        posts, basename_index = _load_posts(path_prefix)
        encoding_issues = scan_markdown_encoding_issues(path_prefix, max_results=1000)
        frontmatter_missing = _frontmatter_missing(posts)
        required_missing = _required_frontmatter_missing(posts, required_frontmatter or [])
        broken_wikilinks = _broken_wikilinks(posts, basename_index)
        suspicious_tags = _suspicious_tag_variants(posts)

        summary = {
            "path_prefix": path_prefix,
            "file_count": len(posts),
            "findings": {
                "frontmatter_missing": len(frontmatter_missing),
                "required_frontmatter_missing": len(required_missing),
                "broken_wikilinks": len(broken_wikilinks),
                "suspicious_tag_variants": len(suspicious_tags),
                "encoding_issues": len(encoding_issues),
            },
            "examples": {
                "frontmatter_missing": frontmatter_missing[:max_examples],
                "required_frontmatter_missing": required_missing[:max_examples],
                "broken_wikilinks": broken_wikilinks[:max_examples],
                "suspicious_tag_variants": suspicious_tags[:max_examples],
                "encoding_issues": encoding_issues[:max_examples],
            },
        }
        return vault_json_dumps(summary)
    except Exception as e:
        return vault_json_dumps({"error": str(e), "path_prefix": path_prefix})


def vault_analytics_findings(
    category: str,
    path_prefix: str = "",
    required_frontmatter: list[str] | None = None,
    max_results: int = 50,
) -> str:
    """Return detailed findings for one analytics category."""
    try:
        posts, basename_index = _load_posts(path_prefix)
        required_frontmatter = required_frontmatter or []
        category_map = {
            "frontmatter_missing": lambda: _frontmatter_missing(posts),
            "required_frontmatter_missing": lambda: _required_frontmatter_missing(posts, required_frontmatter),
            "broken_wikilinks": lambda: _broken_wikilinks(posts, basename_index),
            "suspicious_tag_variants": lambda: _suspicious_tag_variants(posts),
            "encoding_issues": lambda: scan_markdown_encoding_issues(path_prefix, max_results=max_results),
        }
        if category not in category_map:
            return vault_json_dumps(
                {
                    "error": (
                        "Unsupported category. Use one of: frontmatter_missing, "
                        "required_frontmatter_missing, broken_wikilinks, "
                        "suspicious_tag_variants, encoding_issues"
                    ),
                    "category": category,
                }
            )

        findings = category_map[category]()
        return vault_json_dumps(
            {
                "category": category,
                "path_prefix": path_prefix,
                "required_frontmatter": required_frontmatter,
                "count": len(findings),
                "results": findings[:max_results],
                "truncated": len(findings) > max_results,
            }
        )
    except Exception as e:
        return vault_json_dumps({"error": str(e), "category": category, "path_prefix": path_prefix})

#!/usr/bin/env python3
"""
US Law Enforcement Hierarchy & Endpoint Harvester
-------------------------------------------------
Traverses Wikipedia's master list of US state/local law enforcement agencies,
branches through jurisdictional tiers (Statewide, County, Municipal, Special),
and resolves internal Wikipedia articles or official external domains (.gov/.us/.org).
"""

from dataclasses import asdict, dataclass
from enum import Enum
import json
import logging
import re
import sys
import time
from typing import Dict, Generator, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("LEDirectoryHarvester")


class ResolutionType(str, Enum):
    INTERNAL_WIKI = "INTERNAL_WIKI"
    RED_LINK = "RED_LINK"
    UNLINKED_TEXT = "UNLINKED_TEXT"


@dataclass
class ResolvedAgencyNode:
    state_or_territory: str
    jurisdiction_tier: str  # STATEWIDE, COUNTY, MUNICIPAL, SPECIAL, or UNKNOWN
    agency_name: str
    resolution_type: ResolutionType
    wiki_title: Optional[str] = None
    wiki_url: Optional[str] = None
    external_official_url: Optional[str] = None
    source_list_url: Optional[str] = None


class LawEnforcementCrawler:
    BASE_URL = "https://en.wikipedia.org"
    ROOT_INDEX_URL = (
        "https://en.wikipedia.org/wiki/List_of_United_States_state_and_local_law_enforcement_agencies"
    )

    # Patterns matching standard Wikipedia section headers for law enforcement
    TIER_HEADER_PATTERNS = {
        "STATEWIDE": re.compile(
            r"^(State\s+agencies|Statewide|State\s+Police|State\s+Patrol|State\s+Bureaus)",
            re.IGNORECASE,
        ),
        "COUNTY": re.compile(
            r"^(County\s+agencies|Sheriff|County\s+Police|Parish\s+Sheriff)",
            re.IGNORECASE,
        ),
        "MUNICIPAL": re.compile(
            r"^(Municipal\s+agencies|City\s+police|Town\s+marshals|Local\s+agencies)",
            re.IGNORECASE,
        ),
        "SPECIAL": re.compile(
            r"^(Special\s+jurisdiction|College|University|Transit|Port|Airport|Park|Conservation|Tribal)",
            re.IGNORECASE,
        ),
    }

    SUBPAGE_HREF_PATTERN = re.compile(
        r"^/wiki/(List_of_law_enforcement_agencies_in_[A-Za-z_()]+|Law_enforcement_in_[A-Za-z_()]+)$"
    )

    def __init__(self, user_agent: Optional[str] = None, delay_seconds: float = 0.5):
        self.delay_seconds = delay_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or "LawEnforcementDataResearchBot/1.0 (contact: admin@example.org; Educational research on jurisdictional frameworks)"
            }
        )

    def _fetch_soup(self, url: str) -> Optional[BeautifulSoup]:
        """Performs a polite GET request with rate limiting and returns a BeautifulSoup object."""
        try:
            time.sleep(self.delay_seconds)
            response = self.session.get(url, timeout=12)
            if response.status_code == 200:
                return BeautifulSoup(response.text, "html.parser")
            logger.warning("GET failed for %s with status %s", url, response.status_code)
        except requests.RequestException as exc:
            logger.error("Request error connecting to %s: %s", url, exc)
        return None

    def discover_jurisdiction_subpages(self) -> List[Dict[str, str]]:
        """Parses Layer 0 to extract all 50 state, DC, and territorial sub-page URLs."""
        soup = self._fetch_soup(self.ROOT_INDEX_URL)
        if not soup:
            logger.error("Failed to load root index: %s", self.ROOT_INDEX_URL)
            return []

        discovered = {}
        for a_tag in soup.find_all("a", href=self.SUBPAGE_HREF_PATTERN):
            raw_href = a_tag["href"]
            full_url = urljoin(self.BASE_URL, raw_href)

            # Clean name from path
            slug = (
                raw_href.replace("/wiki/List_of_law_enforcement_agencies_in_", "")
                .replace("/wiki/Law_enforcement_in_", "")
                .replace("_", " ")
            )
            # Remove any disambiguation (e.g., 'Washington (state)')
            jurisdiction_name = re.sub(r"\(.*?\)", "", slug).strip()

            if jurisdiction_name and jurisdiction_name not in discovered:
                discovered[jurisdiction_name] = full_url

        logger.info("Discovered %d jurisdiction sub-pages from root.", len(discovered))
        return [
            {"jurisdiction": k, "url": v}
            for k, v in sorted(discovered.items(), key=lambda x: x[0])
        ]

    def _classify_section_heading(self, heading_tag: Tag) -> Optional[str]:
        """Matches an h2/h3 heading against operational classification regex patterns."""
        header_text = heading_tag.get_text(strip=True)
        # Strip Wikipedia '[edit]' tags
        clean_header = re.sub(r"\[edit\]$", "", header_text).strip()

        for tier, pattern in self.TIER_HEADER_PATTERNS.items():
            if pattern.search(clean_header):
                return tier
        return None

    def parse_state_directory(
        self, state_name: str, state_url: str, resolve_external: bool = True
    ) -> Generator[ResolvedAgencyNode, None, None]:
        """
        Traverses a state-level sub-page:
        1. Tracks context based on h2/h3 operational classification headers.
        2. Iterates over <ul>/<li> lists and table rows.
        3. Branches into INTERNAL_WIKI, RED_LINK, or UNLINKED_TEXT.
        4. Queries article infobox / external links if internal.
        """
        soup = self._fetch_soup(state_url)
        if not soup:
            return

        body = soup.find("div", class_="mw-parser-output")
        if not body:
            return

        current_tier = "UNKNOWN"

        for element in body.children:
            if not isinstance(element, Tag):
                continue

            # Update jurisdictional tier context
            if element.name in ["h2", "h3"]:
                tier_match = self._classify_section_heading(element)
                if tier_match:
                    current_tier = tier_match
                elif "see also" in element.get_text(strip=True).lower() or "references" in element.get_text(strip=True).lower():
                    # Terminal sections reached
                    current_tier = "TERMINAL"

            if current_tier == "TERMINAL":
                break

            if current_tier == "UNKNOWN":
                continue

            # Process unordered/ordered lists
            if element.name in ["ul", "ol"]:
                for li in element.find_all("li", recursive=False):
                    node = self._resolve_node_from_element(
                        li, state_name, current_tier, state_url
                    )
                    if node:
                        if resolve_external and node.resolution_type == ResolutionType.INTERNAL_WIKI:
                            node.external_official_url = self.extract_external_website(node.wiki_url)
                        yield node

            # Process structured wikitable rows
            elif element.name == "table" and "wikitable" in element.get("class", []):
                for row in element.find_all("tr")[1:]:  # skip th header
                    cells = row.find_all(["td", "th"])
                    if not cells:
                        continue
                    first_cell = cells[0]
                    node = self._resolve_node_from_element(
                        first_cell, state_name, current_tier, state_url
                    )
                    if node:
                        if resolve_external and node.resolution_type == ResolutionType.INTERNAL_WIKI:
                            node.external_official_url = self.extract_external_website(node.wiki_url)
                        yield node

    def _resolve_node_from_element(
        self, element: Tag, jurisdiction: str, tier: str, source_url: str
    ) -> Optional[ResolvedAgencyNode]:
        """Decision tree: evaluates whether an element contains an active article, red link, or raw text."""
        # Find first relevant anchor tag
        anchor = element.find("a")

        if not anchor:
            raw_text = element.get_text(strip=True)
            if not raw_text or len(raw_text) < 3 or "List of" in raw_text:
                return None
            # Route: UNLINKED_TEXT
            return ResolvedAgencyNode(
                state_or_territory=jurisdiction,
                jurisdiction_tier=tier,
                agency_name=raw_text.split("–")[0].split("-")[0].strip(),
                resolution_type=ResolutionType.UNLINKED_TEXT,
                source_list_url=source_url,
            )

        href = anchor.get("href", "")
        anchor_title = anchor.get("title", "")
        anchor_text = anchor.get_text(strip=True)

        if not anchor_text or "citation needed" in anchor_text.lower():
            return None

        classes = anchor.get("class", [])

        # Route: RED_LINK (Wikipedia article does not exist yet)
        if "new" in classes:
            return ResolvedAgencyNode(
                state_or_territory=jurisdiction,
                jurisdiction_tier=tier,
                agency_name=anchor_text,
                resolution_type=ResolutionType.RED_LINK,
                wiki_title=anchor_title.replace(" (page does not exist)", ""),
                source_list_url=source_url,
            )

        # Route: INTERNAL_WIKI (Active internal page)
        if href.startswith("/wiki/") and not any(
            href.startswith(skip)
            for skip in ["/wiki/File:", "/wiki/Help:", "/wiki/Category:", "/wiki/Special:"]
        ):
            return ResolvedAgencyNode(
                state_or_territory=jurisdiction,
                jurisdiction_tier=tier,
                agency_name=anchor_text,
                resolution_type=ResolutionType.INTERNAL_WIKI,
                wiki_title=anchor_title or href.replace("/wiki/", ""),
                wiki_url=urljoin(self.BASE_URL, href),
                source_list_url=source_url,
            )

        return None

    def extract_external_website(self, wiki_article_url: Optional[str]) -> Optional[str]:
        """
        Extracts an official external web endpoint from an agency's Wikipedia article:
        Branch 1: Inspects the Infobox table for class 'infobox-data' containing an external URL.
        Branch 2: Scans the 'External links' section for authoritative .gov, .us, or .org domains.
        """
        if not wiki_article_url:
            return None

        soup = self._fetch_soup(wiki_article_url)
        if not soup:
            return None

        # Branch 1: Infobox Website Extraction
        infobox = soup.find("table", class_=lambda c: c and "infobox" in c)
        if infobox:
            for row in infobox.find_all("tr"):
                header = row.find(["th", "td"])
                if header and re.search(r"(website|url|web)", header.get_text(strip=True), re.I):
                    ext_anchor = row.find("a", class_="external")
                    if ext_anchor and ext_anchor.get("href"):
                        target_url = ext_anchor["href"]
                        if self._is_valid_agency_endpoint(target_url):
                            return target_url

        # Branch 2: External Links Section Extraction
        ext_heading = soup.find(id=re.compile(r"External_links", re.I))
        if ext_heading:
            parent_container = ext_heading.find_parent(["h2", "h3"])
            if parent_container:
                link_list = parent_container.find_next_sibling(["ul", "ol"])
                if link_list:
                    for anchor in link_list.find_all("a", class_="external"):
                        candidate = anchor.get("href", "")
                        if self._is_valid_agency_endpoint(candidate):
                            return candidate

        return None

    @staticmethod
    def _is_valid_agency_endpoint(url: str) -> bool:
        """Filters out tracking, social platforms, and non-authoritative redirects."""
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        netloc = parsed.netloc.lower()
        disallowed = [
            "facebook.com",
            "twitter.com",
            "x.com",
            "instagram.com",
            "youtube.com",
            "archive.org",
            "odmp.org",
            "wikipedia.org",
        ]
        return not any(domain in netloc for domain in disallowed)


# ----------------------------------------------------------------------
# Execution Entry Point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    crawler = LawEnforcementCrawler(delay_seconds=0.3)

    logger.info("Starting crawler: retrieving jurisdiction lists...")
    jurisdictions = crawler.discover_jurisdiction_subpages()

    # Target specific state for demonstration (e.g., Wyoming)
    target_state = next(
        (item for item in jurisdictions if item["jurisdiction"].lower() == "wyoming"),
        jurisdictions[0] if jurisdictions else None,
    )

    if not target_state:
        logger.error("No valid jurisdiction targets found.")
        sys.exit(1)

    logger.info(
        "Parsing target state: '%s' (%s)",
        target_state["jurisdiction"],
        target_state["url"],
    )

    records: List[Dict] = []
    # Set resolve_external=True to execute full traversal into child Wikipedia articles
    for agency_node in crawler.parse_state_directory(
        target_state["jurisdiction"], target_state["url"], resolve_external=True
    ):
        records.append(asdict(agency_node))
        logger.info(
            "Found [%s - %s] -> %s (Type: %s, Web: %s)",
            agency_node.jurisdiction_tier,
            agency_node.agency_name,
            agency_node.wiki_url or "[No Internal Page]",
            agency_node.resolution_type.value,
            agency_node.external_official_url or "[None]",
        )

    # Output structured harvest results
    output_filename = f"law_enforcement_{target_state['jurisdiction'].lower().replace(' ', '_')}.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)

    logger.info("Successfully exported %d agency nodes to %s", len(records), output_filename)

.

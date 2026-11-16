"""
Internal linking agent.

Three phases, each independently runnable from the CLI:
    crawl   -> refresh the owned-domain link graph (table: internal_links)
    detect  -> run the two detectors and persist suggestions
                 (table: internal_link_suggestions)
    report  -> export suggestions as Excel workbooks (one per owned domain)

Weekly schedule: crawl -> detect -> report, serially.
"""
AGENT_NAME = "content_operations.internal_linker"

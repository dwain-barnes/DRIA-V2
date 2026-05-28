---
name: internet_search
description: Use this skill to search the internet through the local SearXNG instance for current facts, news, prices, people, products, or anything that may have changed recently.
action: searxng
---

# Internet Search

Use this skill when the user asks for current, recent, online, or externally verifiable information.

Build a focused search query from the user's question. Use `news` as the category for news or recent events, and `general` for most other searches. Use a recency filter only when the user asks for current, recent, today, this week, this month, or latest information.

After results return, answer from the snippets and mention source names or domains when useful. Do not include raw URLs in spoken answers. If the search results are weak or contradictory, say that clearly.

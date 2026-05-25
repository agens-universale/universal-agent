---
id: using-articles
name: Using Articles
description: "Authoring of full Russian Markdown articles — create a new article from a subject/brief, or fact-check and revise an existing one."
version: 1.0.0
---

# Using Articles

This pack authors and fact-checks full Russian Markdown articles.

## Pick the inner skill by what you must deliver

| Deliverable | skills_load |
|---|---|
| A new publication-ready article from a subject or brief | `articles:article-creator` |
| A verified/revised version of an existing article or draft | `articles:check-article` |

## Optional handoff

After creating an article, you may offer the user to fact-check it. If
they agree, load `articles:check-article` next. This is optional —
`check-article` works on any article the user provides, not only output
of `article-creator`.

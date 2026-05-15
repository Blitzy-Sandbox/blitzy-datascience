# blitzy-datascience

A modular Python pipeline for ingesting, normalizing, and persisting NBA
statistics data from the NBA Stats API.

## About

`blitzy-datascience` is a CLI-driven data ingestion pipeline that pulls
comprehensive NBA statistics from the NBA Stats API (stats.nba.com). It
covers six data domains — players, teams, games, lineups, tracking, and
schedule — across 15+ API endpoints.

The pipeline normalizes all API responses into flat, schema-consistent
CSV files suitable for downstream analytics, dashboarding, and BI tools.
It supports configurable season targeting (default: 2025-26),
checkpoint-based resumability, and exponential backoff to respect API
rate limits.

Each layer (HTTP client, endpoints, pipelines, storage) is independent
and swappable. The storage interface supports CSV out of the box, with
a pluggable design for future database backends.

## Quick links

- [New Product Prompt (2026-04-18)](New_Product_Prompt_20260418.md)
- [Source on GitHub](https://github.com/Blitzy-Sandbox/blitzy-datascience)

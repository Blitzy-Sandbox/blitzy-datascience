"""Pluggable storage package for the NBA Data Ingestion Pipeline.

This package implements Feature F-006 (CSV Writer with Pluggable Interface)
and is the enforcement point for Operational Rule 7 (Pluggable Storage).

Public entry points live in submodules; nothing is re-exported here so that
callers make their dependency on a specific backend explicit::

    from storage.csv_writer import BaseWriter, CSVWriter

The abstract ``BaseWriter`` interface is preserved for future database,
object-storage, or columnar backends. Only the ``CSVWriter`` concrete
implementation ships in this release; see
``docs/New_Product_Prompt_20260418.md`` §5 Rule 7 and the Agent Action Plan
§0.6.2.2 for the list of deliberately-excluded backends.
"""

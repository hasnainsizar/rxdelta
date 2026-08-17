"""Snapshot comparison and member impact estimation.

This package reads stored snapshots and shared domain types only. It never
imports from rxdelta.ingest, so the comparison logic survives a change of
source dataset.
"""

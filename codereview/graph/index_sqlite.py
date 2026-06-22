from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..utils.paths import ensure_dir


def rebuild_sqlite_index(graph: dict, db_path: Path) -> None:
    ensure_dir(db_path.parent)
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            DROP TABLE IF EXISTS nodes;
            DROP TABLE IF EXISTS edges;
            CREATE TABLE nodes (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              file TEXT,
              name TEXT,
              qualified_name TEXT,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE edges (
              id TEXT PRIMARY KEY,
              source_id TEXT NOT NULL,
              target_id TEXT NOT NULL,
              edge_type TEXT NOT NULL,
              confidence TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE INDEX idx_edges_source ON edges(source_id);
            CREATE INDEX idx_edges_target ON edges(target_id);
            CREATE INDEX idx_nodes_file ON nodes(file);
            CREATE INDEX idx_nodes_name ON nodes(name);
            """
        )
        db.executemany(
            "INSERT OR REPLACE INTO nodes (id, kind, file, name, qualified_name, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    str(node.get("id") or ""),
                    str(node.get("kind") or ""),
                    str(node.get("file") or ""),
                    str(node.get("name") or ""),
                    str(node.get("qualified_name") or ""),
                    json.dumps(node, ensure_ascii=False, sort_keys=True),
                )
                for node in graph.get("nodes", [])
                if isinstance(node, dict) and node.get("id")
            ],
        )
        db.executemany(
            "INSERT OR REPLACE INTO edges (id, source_id, target_id, edge_type, confidence, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    str(edge.get("id") or ""),
                    str(edge.get("from") or ""),
                    str(edge.get("to") or ""),
                    str(edge.get("type") or ""),
                    str(edge.get("confidence") or ""),
                    json.dumps(edge, ensure_ascii=False, sort_keys=True),
                )
                for edge in graph.get("edges", [])
                if isinstance(edge, dict) and edge.get("id")
            ],
        )
        db.commit()

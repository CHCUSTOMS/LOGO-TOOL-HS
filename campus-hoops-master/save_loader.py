"""
Campus Hoops save file loader and exploration utilities.

Usage in PyCharm Python console or a script:
    from save_loader import SaveFile
    save = SaveFile("saint_francis_pa_season_6_2026-05-10T150915705711")
    save.describe()                        # top-level structure
    save.describe("teams")                 # structure of a specific key
    save.get("teams")                      # raw value at a key path
    save.get("teams.saint_francis_pa")     # dot-path navigation
    save.search("championship")            # find all keys matching a pattern
    save.keys("teams.saint_francis_pa")    # list keys at a path

    # List exploration
    save.peek("recruitingPool")            # show first 5 items' fields + values
    save.peek("recruitingPool", n=10)      # show first N items
    save.find("recruitingPool", position="PG")          # filter by field equality
    save.find("recruitingPool", rating=lambda r: r>=90) # filter by predicate
    df = save.to_df("recruitingPool")      # pandas DataFrame for sorting/analysis
"""

import gzip
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Callable


class SaveFile:
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.meta: dict = self._load_json("meta.json")
        self.manifest: dict = self._load_json("manifest.json")
        self._session: dict | None = None   # lazy-loaded; it's large
        self._history: sqlite3.Connection | None = None  # lazy-loaded

        print(f"Loaded save: {self.meta.get('saveName')} — {self.meta.get('teamName')}")
        print(f"  Season: {self.meta.get('seasonYear')}  |  Format v{self.meta.get('saveFormatVersion')}")
        print(f"  Last saved: {self.meta.get('lastSaved')}")
        print()
        print("session data is lazy-loaded. Access via save.session or any save.get() call.")

    # ------------------------------------------------------------------ loading

    def _load_json(self, filename: str) -> dict:
        path = self.folder / filename
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @property
    def session(self) -> dict:
        if self._session is None:
            print("Loading session.json (this may take a moment for large saves)...")
            # Prefer the decompressed file; fall back to .gz
            plain = self.folder / "session.json" / "session.json"
            gz = self.folder / "session.json.gz"
            if plain.exists():
                with open(plain, "r", encoding="utf-8") as f:
                    self._session = json.load(f)
            elif gz.exists():
                with gzip.open(gz, "rt", encoding="utf-8") as f:
                    self._session = json.load(f)
            else:
                raise FileNotFoundError("session.json not found in save folder")
            print("Session loaded.")
        return self._session

    # ------------------------------------------------------------------ navigation

    def get(self, path: str = "", default: Any = None) -> Any:
        """
        Navigate the session data with a dot-separated path.
        E.g. save.get("teams.saint_francis_pa.roster")
        Returns the raw Python object at that location.
        """
        node = self.session
        if not path:
            return node
        for part in path.split("."):
            if isinstance(node, dict):
                if part not in node:
                    return default
                node = node[part]
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return node

    def keys(self, path: str = "") -> list[str]:
        """List the keys / indices at a given path."""
        node = self.get(path)
        if isinstance(node, dict):
            return list(node.keys())
        if isinstance(node, list):
            return list(range(len(node)))
        return []

    # ------------------------------------------------------------------ structure exploration

    def describe(self, path: str = "", max_depth: int = 3, _indent: int = 0) -> None:
        """
        Print a recursive summary of the data structure at `path`.
        Shows type, length, and nested keys up to max_depth.
        """
        node = self.get(path) if path else self.session
        prefix = "  " * _indent
        label = f"[{path}] " if path and _indent == 0 else ""

        if isinstance(node, dict):
            print(f"{prefix}{label}dict ({len(node)} keys)")
            if _indent < max_depth:
                for k, v in node.items():
                    self._describe_node(v, k, _indent + 1, max_depth)
        elif isinstance(node, list):
            print(f"{prefix}{label}list ({len(node)} items)")
            if _indent < max_depth and node:
                self._describe_node(node[0], "[0]", _indent + 1, max_depth)
        else:
            print(f"{prefix}{label}{type(node).__name__}: {repr(node)[:120]}")

    def _describe_node(self, node: Any, label: str, indent: int, max_depth: int) -> None:
        prefix = "  " * indent
        if isinstance(node, dict):
            print(f"{prefix}{label}: dict ({len(node)} keys)")
            if indent < max_depth:
                for k, v in node.items():
                    self._describe_node(v, k, indent + 1, max_depth)
        elif isinstance(node, list):
            item_type = type(node[0]).__name__ if node else "empty"
            print(f"{prefix}{label}: list ({len(node)} items of {item_type})")
            if indent < max_depth and node:
                self._describe_node(node[0], "[0]", indent + 1, max_depth)
        else:
            print(f"{prefix}{label}: {type(node).__name__} = {repr(node)[:100]}")

    # ------------------------------------------------------------------ search

    def search(
        self,
        pattern: str,
        path: str = "",
        *,
        search_values: bool = False,
        max_results: int = 50,
    ) -> list[str]:
        """
        Find all key paths under `path` whose key name matches `pattern` (regex).
        If search_values=True, also matches string values.
        Returns a list of dot-separated path strings.
        """
        regex = re.compile(pattern, re.IGNORECASE)
        results: list[str] = []
        root = self.get(path) if path else self.session
        base = path + "." if path else ""
        self._search_recursive(root, base, regex, search_values, results, max_results)
        for r in results:
            print(r)
        return results

    def _search_recursive(
        self,
        node: Any,
        current_path: str,
        regex: re.Pattern,
        search_values: bool,
        results: list[str],
        max_results: int,
    ) -> None:
        if len(results) >= max_results:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = f"{current_path}{k}"
                if regex.search(str(k)):
                    results.append(child_path)
                    if len(results) >= max_results:
                        return
                self._search_recursive(v, child_path + ".", regex, search_values, results, max_results)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                self._search_recursive(item, f"{current_path}{i}.", regex, search_values, results, max_results)
        elif search_values and isinstance(node, str):
            if regex.search(node):
                results.append(f"{current_path.rstrip('.')} = {repr(node)[:80]}")

    # ------------------------------------------------------------------ list exploration

    def peek(self, path: str, n: int = 5) -> list[dict]:
        """
        Print the fields and values of the first `n` items in a list.
        Great for understanding what a recruiting pool or roster looks like.
        """
        items = self.get(path)
        if not isinstance(items, list):
            print(f"{path} is not a list (got {type(items).__name__})")
            return []

        sample = items[:n]
        print(f"{path}: {len(items)} total items — showing first {len(sample)}\n")

        for i, item in enumerate(sample):
            print(f"  [{i}]", end=" ")
            if isinstance(item, dict):
                print(f"({len(item)} fields)")
                for k, v in item.items():
                    if isinstance(v, (dict, list)):
                        summary = f"{type(v).__name__}({len(v)})"
                    else:
                        summary = repr(v)
                    print(f"       {k}: {summary[:80]}")
            else:
                print(repr(item)[:120])
            print()

        return sample

    def find(self, path: str, **filters) -> list[Any]:
        """
        Filter a list at `path` by field values or predicates.

        Equality:   save.find("recruitingPool", position="PG")
        Predicate:  save.find("recruitingPool", rating=lambda r: r >= 90)
        Combined:   save.find("recruitingPool", position="PG", rating=lambda r: r >= 85)

        Returns the matching items. Also prints a count and the first 20 results.
        """
        items = self.get(path)
        if not isinstance(items, list):
            print(f"{path} is not a list")
            return []

        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            match = True
            for field, criterion in filters.items():
                val = item.get(field)
                if callable(criterion):
                    if not criterion(val):
                        match = False
                        break
                else:
                    if val != criterion:
                        match = False
                        break
            if match:
                results.append(item)

        print(f"Found {len(results)} / {len(items)} items matching {filters}")
        for item in results[:20]:
            print(" ", {k: v for k, v in item.items() if not isinstance(v, (dict, list))})
        if len(results) > 20:
            print(f"  ... and {len(results) - 20} more")

        return results

    def to_df(self, path: str):
        """
        Convert a list of dicts at `path` into a pandas DataFrame.
        Nested dicts/lists are dropped; scalar fields become columns.
        Requires pandas (pip install pandas).
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("pip install pandas")

        items = self.get(path)
        if not isinstance(items, list):
            raise ValueError(f"{path} is not a list")

        flat = []
        for item in items:
            if isinstance(item, dict):
                flat.append({k: v for k, v in item.items() if not isinstance(v, (dict, list))})
            else:
                flat.append({"value": item})

        df = pd.DataFrame(flat)
        print(f"{path}: DataFrame with {len(df)} rows × {len(df.columns)} columns")
        print(f"  Columns: {list(df.columns)}")
        return df

    # ------------------------------------------------------------------ convenience queries

    def top_level_keys(self) -> list[str]:
        """Return and print all top-level keys in session.json."""
        ks = list(self.session.keys())
        for k in ks:
            v = self.session[k]
            if isinstance(v, dict):
                print(f"  {k}: dict ({len(v)} keys)")
            elif isinstance(v, list):
                print(f"  {k}: list ({len(v)} items)")
            else:
                print(f"  {k}: {type(v).__name__} = {repr(v)[:80]}")
        return ks

    # ------------------------------------------------------------------ history DB

    @property
    def history(self) -> sqlite3.Connection:
        """Cached connection to history.db."""
        if self._history is None:
            db_path = self.folder / "history.db"
            if not db_path.exists():
                raise FileNotFoundError("history.db not found in save folder")
            self._history = sqlite3.connect(str(db_path), check_same_thread=False)
            self._history.row_factory = sqlite3.Row
        return self._history

    def history_db(self) -> sqlite3.Connection:
        """Open history.db and print table names. Use save.history for scripted access."""
        conn = self.history
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print("Tables:", [t["name"] for t in tables])
        return conn

    # ------------------------------------------------------------------ save / export

    def save_session(self, output_path: str | Path | None = None) -> Path:
        """
        Write the (possibly modified) session data back to a JSON file.
        Defaults to session_modified.json in the save folder.
        """
        if self._session is None:
            raise RuntimeError("Session not loaded — nothing to save.")
        out = Path(output_path) if output_path else self.folder / "session_modified.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(self._session, f)
        print(f"Saved to {out}")
        return out

    # ------------------------------------------------------------------ modding

    def set(self, path: str, value: Any) -> None:
        """Set a value in session data at a dot-separated path."""
        parts = path.split(".")
        node = self.session  # ensures session is loaded
        for part in parts[:-1]:
            if isinstance(node, dict):
                node = node[part]
            elif isinstance(node, list):
                node = node[int(part)]
            else:
                raise KeyError(f"Cannot navigate into {type(node).__name__} at '{part}'")
        last = parts[-1]
        if isinstance(node, dict):
            node[last] = value
        elif isinstance(node, list):
            node[int(last)] = value
        else:
            raise KeyError(f"Cannot set on {type(node).__name__}")

    def to_campushoops_bytes(
        self,
        source_zip_bytes: bytes,
        logo_overrides: dict[str, bytes] | None = None,
    ) -> bytes:
        """Repackage save as .campushoops (zip) bytes with the modified session.json.gz.

        logo_overrides: team_id -> PNG bytes; replaces or adds logos/{team_id}.png.
        """
        if self._session is None:
            raise RuntimeError("Session not loaded — nothing to export.")

        # Serialize + gzip-compress the modified session
        raw = json.dumps(self._session, separators=(",", ":")).encode("utf-8")
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
            gz.write(raw)
        gz_bytes = gz_buf.getvalue()

        # Detect folder prefix inside the source zip (e.g. "save_name/")
        src = zipfile.ZipFile(io.BytesIO(source_zip_bytes), "r")
        prefix = ""
        for info in src.infolist():
            if info.filename.endswith("meta.json"):
                prefix = info.filename[: -len("meta.json")]
                break

        # Paths being replaced by logo_overrides — skip originals
        override_paths: set[str] = set()
        if logo_overrides:
            override_paths = {f"{prefix}logos/{tid}.png" for tid in logo_overrides}

        # Build the output zip, swapping in the new session and any logo overrides
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in src.infolist():
                parts = [p for p in info.filename.split("/") if p]
                if any(p in ("session.json", "session.json.gz") for p in parts):
                    continue
                if info.filename in override_paths:
                    continue
                zout.writestr(info, src.read(info.filename))
            zout.writestr(f"{prefix}session.json.gz", gz_bytes)
            if logo_overrides:
                for tid, png_bytes in logo_overrides.items():
                    zout.writestr(f"{prefix}logos/{tid}.png", png_bytes)

        src.close()
        return out.getvalue()

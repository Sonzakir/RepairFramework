from importlib.util import find_spec
from pathlib import Path

spec = find_spec("fauxpy")
if spec is None or spec.origin is None:
    raise SystemExit("Cannot patch FauxPy because the fauxpy package is not importable.")

package_root = Path(spec.origin).parent
sbfl_root = package_root / "fault_localization" / "sbfl"


def read(path):
    return path.read_text(encoding="utf-8")


def write(path, text):
    path.write_text(text, encoding="utf-8")


def replace_once(text, old, new, label, marker=None):
    if (marker is not None and marker in text) or new in text:
        return text
    if old not in text:
        raise SystemExit(f"Cannot apply FauxPy SBFL metric patch: expected {label} was not found.")
    return text.replace(old, new, 1)


# --- Jaccard metric (known literature metric missing from FauxPy 0.7.0) ---
metric_path = sbfl_root / "metric_jaccard.py"
write(
    metric_path,
    '''class MetricJaccard:
    def __init__(self, epsilon: float):
        self._metric_name = "Jaccard"
        self._epsilon = epsilon

    def get_metric_name(self):
        return self._metric_name

    def compute(self, ef, ep, nf, np):
        score = float(ef) / (ef + ep + nf + self._epsilon)
        return score
''',
)

# --- Weighted SBI metric (WSBI — custom metric) ---
# _WSBI_ALPHA is injected by the caller (fauxpy.py) as a prepended assignment.
# It is baked into the written file so the value is fixed at patch time.
metric_path = sbfl_root / "metric_wsbi.py"
write(
    metric_path,
    f'''class MetricWSBI:
    """Weighted SBI: ef / (ef + ALPHA * ep).

    Unlike plain SBI (ALPHA=1), a passing test covering a statement counts only
    ALPHA as much as a failing test, reflecting the asymmetry that failing tests
    are stronger evidence of a fault than passing tests are of correctness.
    ALPHA is configurable at patch time via --wsbi-alpha (default 0.5).
    """

    ALPHA = {_WSBI_ALPHA!r}

    def __init__(self, epsilon: float):
        self._metric_name = "WSBI"
        self._epsilon = epsilon

    def get_metric_name(self):
        return self._metric_name

    def compute(self, ef, ep, nf, np):
        denominator = ef + self.ALPHA * ep
        if denominator == 0:
            return 0.0
        return float(ef) / denominator
''',
)

# --- Patch ranking_metric_manager.py to register Jaccard and WSBI ---
ranking_path = sbfl_root / "ranking_metric_manager.py"
ranking_text = read(ranking_path)
ranking_text = replace_once(
    ranking_text,
    "from fauxpy.fault_localization.sbfl.metric_dstar import MetricDstar\n",
    "from fauxpy.fault_localization.sbfl.metric_dstar import MetricDstar\n"
    "from fauxpy.fault_localization.sbfl.metric_jaccard import MetricJaccard\n",
    "ranking metric imports",
    "from fauxpy.fault_localization.sbfl.metric_jaccard import MetricJaccard\n",
)
ranking_text = replace_once(
    ranking_text,
    "from fauxpy.fault_localization.sbfl.metric_jaccard import MetricJaccard\n",
    "from fauxpy.fault_localization.sbfl.metric_jaccard import MetricJaccard\n"
    "from fauxpy.fault_localization.sbfl.metric_wsbi import MetricWSBI\n",
    "ranking WSBI metric import",
    "from fauxpy.fault_localization.sbfl.metric_wsbi import MetricWSBI\n",
)
ranking_text = replace_once(
    ranking_text,
    "            MetricDstar(self.EPSILON),\n        ]",
    "            MetricDstar(self.EPSILON),\n"
    "            MetricJaccard(self.EPSILON),\n"
    "        ]",
    "ranking metric list",
    "            MetricJaccard(self.EPSILON),\n",
)
ranking_text = replace_once(
    ranking_text,
    "            MetricJaccard(self.EPSILON),\n",
    "            MetricJaccard(self.EPSILON),\n"
    "            MetricWSBI(self.EPSILON),\n",
    "ranking WSBI metric list",
    "            MetricWSBI(self.EPSILON),\n",
)
write(ranking_path, ranking_text)

# --- Patch db_manager.py to add Jaccard and WSBI columns ---
db_path = sbfl_root / "db_manager.py"
db_text = read(db_path)

# Schema: add Jaccard column
db_text = replace_once(
    db_text,
    '            f"Dstar REAL NOT NULL);"\n',
    '            f"Dstar REAL NOT NULL, "\n'
    '            f"Jaccard REAL NOT NULL);"\n',
    "score table schema",
    '            f"Jaccard REAL NOT NULL',
)
# Schema: add WSBI column
db_text = replace_once(
    db_text,
    '            f"Jaccard REAL NOT NULL);"\n',
    '            f"Jaccard REAL NOT NULL, "\n'
    '            f"WSBI REAL NOT NULL);"\n',
    "WSBI score table schema",
    '            f"WSBI REAL NOT NULL',
)
# Indexes: add Jaccard index
db_text = replace_once(
    db_text,
    '        score_dstar_table_index_command = (\n'
    '            f"CREATE INDEX index_Dstar ON {self._Score_table} (Dstar);"\n'
    '        )\n',
    '        score_dstar_table_index_command = (\n'
    '            f"CREATE INDEX index_Dstar ON {self._Score_table} (Dstar);"\n'
    '        )\n'
    '\n'
    '        score_jaccard_table_index_command = (\n'
    '            f"CREATE INDEX index_Jaccard ON {self._Score_table} (Jaccard);"\n'
    '        )\n',
    "score indexes",
    "score_jaccard_table_index_command",
)
# Indexes: add WSBI index
db_text = replace_once(
    db_text,
    '        score_jaccard_table_index_command = (\n'
    '            f"CREATE INDEX index_Jaccard ON {self._Score_table} (Jaccard);"\n'
    '        )\n',
    '        score_jaccard_table_index_command = (\n'
    '            f"CREATE INDEX index_Jaccard ON {self._Score_table} (Jaccard);"\n'
    '        )\n'
    '\n'
    '        score_wsbi_table_index_command = (\n'
    '            f"CREATE INDEX index_WSBI ON {self._Score_table} (WSBI);"\n'
    '        )\n',
    "WSBI score index",
    "score_wsbi_table_index_command",
)
# Schema commands list: add Jaccard
db_text = replace_once(
    db_text,
    "            score_dstar_table_index_command,\n"
    "            view_create_command,\n",
    "            score_dstar_table_index_command,\n"
    "            score_jaccard_table_index_command,\n"
    "            view_create_command,\n",
    "schema command list",
    "            score_jaccard_table_index_command,\n",
)
# Schema commands list: add WSBI
db_text = replace_once(
    db_text,
    "            score_jaccard_table_index_command,\n"
    "            view_create_command,\n",
    "            score_jaccard_table_index_command,\n"
    "            score_wsbi_table_index_command,\n"
    "            view_create_command,\n",
    "WSBI schema command list",
    "            score_wsbi_table_index_command,\n",
)
# Insert placeholders: add one for Jaccard
db_text = replace_once(
    db_text,
    '            f"VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",\n',
    '            f"VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",\n',
    "score insert placeholders",
    '            f"VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?',
)
# Insert placeholders: add one for WSBI
db_text = replace_once(
    db_text,
    '            f"VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",\n',
    '            f"VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",\n',
    "WSBI score insert placeholders",
)
# Insert values: add Jaccard
db_text = replace_once(
    db_text,
    '                scores["Dstar"],\n'
    "            ),\n",
    '                scores["Dstar"],\n'
    '                scores["Jaccard"],\n'
    "            ),\n",
    "score insert values",
    '                scores["Jaccard"],\n',
)
# Insert values: add WSBI
db_text = replace_once(
    db_text,
    '                scores["Jaccard"],\n'
    "            ),\n",
    '                scores["Jaccard"],\n'
    '                scores["WSBI"],\n'
    "            ),\n",
    "WSBI score insert values",
    '                scores["WSBI"],\n',
)
# Top-N queries: add Jaccard
db_text = replace_once(
    db_text,
    '        cur.execute(\n'
    '            f"SELECT Entity, Dstar FROM {self._Score_table} ORDER BY Dstar DESC LIMIT ?",\n'
    '            (top_n,),\n'
    '        )\n'
    '        score_dstar = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '        }\n',
    '        cur.execute(\n'
    '            f"SELECT Entity, Dstar FROM {self._Score_table} ORDER BY Dstar DESC LIMIT ?",\n'
    '            (top_n,),\n'
    '        )\n'
    '        score_dstar = cur.fetchall()\n'
    '\n'
    '        cur.execute(\n'
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC LIMIT ?",\n'
    '            (top_n,),\n'
    '        )\n'
    '        score_jaccard = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '            "Jaccard": score_jaccard,\n'
    '        }\n',
    "top-n Jaccard query",
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC LIMIT ?",\n',
)
# Top-N queries: add WSBI
db_text = replace_once(
    db_text,
    '        cur.execute(\n'
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC LIMIT ?",\n'
    '            (top_n,),\n'
    '        )\n'
    '        score_jaccard = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '            "Jaccard": score_jaccard,\n'
    '        }\n',
    '        cur.execute(\n'
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC LIMIT ?",\n'
    '            (top_n,),\n'
    '        )\n'
    '        score_jaccard = cur.fetchall()\n'
    '\n'
    '        cur.execute(\n'
    '            f"SELECT Entity, WSBI FROM {self._Score_table} ORDER BY WSBI DESC LIMIT ?",\n'
    '            (top_n,),\n'
    '        )\n'
    '        score_wsbi = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '            "Jaccard": score_jaccard,\n'
    '            "WSBI": score_wsbi,\n'
    '        }\n',
    "top-n WSBI query",
    '            f"SELECT Entity, WSBI FROM {self._Score_table} ORDER BY WSBI DESC LIMIT ?",\n',
)
# All-ranks queries: add Jaccard
db_text = replace_once(
    db_text,
    '        cur.execute(\n'
    '            f"SELECT Entity, Dstar FROM {self._Score_table} ORDER BY Dstar DESC"\n'
    '        )\n'
    '        score_dstar = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '        }\n',
    '        cur.execute(\n'
    '            f"SELECT Entity, Dstar FROM {self._Score_table} ORDER BY Dstar DESC"\n'
    '        )\n'
    '        score_dstar = cur.fetchall()\n'
    '\n'
    '        cur.execute(\n'
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC"\n'
    '        )\n'
    '        score_jaccard = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '            "Jaccard": score_jaccard,\n'
    '        }\n',
    "all-ranks Jaccard query",
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC"\n',
)
# All-ranks queries: add WSBI
db_text = replace_once(
    db_text,
    '        cur.execute(\n'
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC"\n'
    '        )\n'
    '        score_jaccard = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '            "Jaccard": score_jaccard,\n'
    '        }\n',
    '        cur.execute(\n'
    '            f"SELECT Entity, Jaccard FROM {self._Score_table} ORDER BY Jaccard DESC"\n'
    '        )\n'
    '        score_jaccard = cur.fetchall()\n'
    '\n'
    '        cur.execute(\n'
    '            f"SELECT Entity, WSBI FROM {self._Score_table} ORDER BY WSBI DESC"\n'
    '        )\n'
    '        score_wsbi = cur.fetchall()\n'
    '\n'
    '        ranked_entities = {\n'
    '            "Tarantula": score_tarantula,\n'
    '            "Ochiai": score_ochiai,\n'
    '            "Dstar": score_dstar,\n'
    '            "Jaccard": score_jaccard,\n'
    '            "WSBI": score_wsbi,\n'
    '        }\n',
    "all-ranks WSBI query",
    '            f"SELECT Entity, WSBI FROM {self._Score_table} ORDER BY WSBI DESC"\n',
)
write(db_path, db_text)
print("FauxPy SBFL metric patch applied.")

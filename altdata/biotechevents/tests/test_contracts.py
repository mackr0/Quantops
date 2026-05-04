"""Source-inspection contract tests guarding architectural invariants."""

import inspect

import biotechevents.scrape_clinicaltrials as sc
import biotechevents.store as st


class TestScraperContracts:
    def test_user_agent_has_email(self):
        assert "@" in sc.USER_AGENT, (
            "User-Agent must include contact email — public-records "
            "etiquette and matches the pattern from edgar13f."
        )

    def test_has_rate_limit_detection(self):
        src = inspect.getsource(sc)
        assert "429" in src and "403" in src
        assert "RateLimitedError" in src

    def test_politeness_delay(self):
        assert sc.REQUEST_DELAY_SEC >= 1.0

    def test_raw_stored_before_parse(self):
        """raw_filing inserted BEFORE upsert_trial — AST-based check
        so docstring mentions don't false-positive the ordering."""
        import ast
        src = inspect.getsource(sc.fetch_recently_updated)
        tree = ast.parse(src)

        def call_names_in_order(node):
            """Yield (lineno, function_name) for every Call in the body,
            in textual order. Docstrings are AST Expr-Constant, not Calls,
            so they're naturally excluded."""
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Name):
                        yield child.lineno, func.id
                    elif isinstance(func, ast.Attribute):
                        yield child.lineno, func.attr

        order = list(call_names_in_order(tree))
        # Find first lineno of insert_raw_filing vs upsert_trial
        raw_lines = [ln for ln, name in order if name == "insert_raw_filing"]
        upsert_lines = [ln for ln, name in order if name == "upsert_trial"]
        assert raw_lines, "insert_raw_filing call not found in fetch_recently_updated"
        assert upsert_lines, "upsert_trial call not found in fetch_recently_updated"
        assert min(raw_lines) < min(upsert_lines), (
            "insert_raw_filing must be invoked BEFORE upsert_trial — otherwise "
            "a parse crash loses the raw JSON and we'd need to re-scrape."
        )

    def test_parser_version_tagged(self):
        assert hasattr(sc, "PARSER_VERSION")
        src = inspect.getsource(sc.fetch_recently_updated)
        assert "PARSER_VERSION" in src

    def test_per_page_commit(self):
        src = inspect.getsource(sc.fetch_recently_updated)
        assert "db_conn.commit()" in src

    def test_pagination_handled(self):
        """If we don't page, we miss data past the first 200 results."""
        src = inspect.getsource(sc.fetch_recently_updated)
        assert "nextPageToken" in src


class TestStoreContracts:
    def test_change_detection_in_upsert_trial(self):
        """The 'phase 2 → phase 3' signal lives entirely in upsert_trial.
        If this regresses (e.g. someone removes the comparison loop),
        downstream consumers silently miss every transition."""
        src = inspect.getsource(st.upsert_trial)
        assert "trial_changes" in src
        assert "phase" in src and "overall_status" in src

    def test_raw_filings_table_exists(self):
        assert "CREATE TABLE IF NOT EXISTS raw_filings" in st.SCHEMA

    def test_unique_constraint_allows_cross_source_external_ids(self):
        """clinicaltrials and fda can both have external_id='X1' without
        colliding — UNIQUE includes source."""
        assert "UNIQUE (source, external_id)" in st.SCHEMA

    def test_migrations_idempotent(self):
        src = inspect.getsource(st._apply_migrations)
        assert "duplicate column" in src.lower()

    def test_parser_version_column_in_trials(self):
        assert "parser_version" in st.SCHEMA.split("trials")[1][:1000]


class TestNormalizerContracts:
    def test_phase_map_covers_canonical_forms(self):
        """The map must handle 'Phase 2', 'PHASE 2', 'PHASE II' all
        landing on 'PHASE2'. Real ClinicalTrials data uses all three."""
        from biotechevents.normalize import normalize_phase
        for variant in ("Phase 2", "PHASE 2", "PHASE II", "PHASE2"):
            assert normalize_phase(variant) == "PHASE2", (
                f"normalize_phase failed on real-world variant: {variant!r}"
            )

    def test_sponsor_map_has_critical_names(self):
        """Specific names that DEFINITELY matter for biotech signal —
        regression guard against accidental deletion."""
        from biotechevents.normalize import sponsor_to_ticker
        for name, expected in [
            ("Moderna", "MRNA"),
            ("Pfizer Inc", "PFE"),
            ("AstraZeneca", "AZN"),
            ("Vertex Pharmaceuticals", "VRTX"),
            ("Regeneron", "REGN"),
        ]:
            assert sponsor_to_ticker(name) == expected, (
                f"Critical sponsor missing from map: {name} → {expected}"
            )

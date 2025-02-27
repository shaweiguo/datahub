import logging
import re
import unittest
import unittest.mock
from abc import ABCMeta, abstractmethod
from typing import List, Set

import sqlparse
from networkx import DiGraph
from sqllineage.core import LineageAnalyzer
from sqllineage.core.holders import Column, SQLLineageHolder

import datahub.utilities.sqllineage_patch

try:
    from sql_metadata import Parser as MetadataSQLParser
except ImportError:
    pass

logger = logging.getLogger(__name__)


class SQLParser(metaclass=ABCMeta):
    def __init__(self, sql_query: str) -> None:
        self._sql_query = sql_query

    @abstractmethod
    def get_tables(self) -> List[str]:
        pass

    @abstractmethod
    def get_columns(self) -> List[str]:
        pass


class MetadataSQLSQLParser(SQLParser):
    _DATE_SWAP_TOKEN = "__d_a_t_e"

    def __init__(self, sql_query: str) -> None:
        super().__init__(sql_query)

        original_sql_query = sql_query

        # MetadataSQLParser makes mistakes on lateral flatten queries, use the prefix
        if "lateral flatten" in sql_query:
            sql_query = sql_query[: sql_query.find("lateral flatten")]

        # MetadataSQLParser also makes mistakes on columns called "date", rename them
        sql_query = re.sub(r"\sdate\s", f" {self._DATE_SWAP_TOKEN} ", sql_query)

        # MetadataSQLParser does not handle "encode" directives well. Remove them
        sql_query = re.sub(r"\sencode [a-zA-Z]*", "", sql_query)

        if sql_query != original_sql_query:
            logger.debug(f"rewrote original query {original_sql_query} as {sql_query}")

        self._parser = MetadataSQLParser(sql_query)

    def get_tables(self) -> List[str]:
        result = self._parser.tables
        # Sort tables to make the list deterministic
        result.sort()
        return result

    def get_columns(self) -> List[str]:
        columns_dict = self._parser.columns_dict
        # don't attempt to parse columns if there are joins involved
        if columns_dict.get("join", {}) != {}:
            return []

        columns_alias_dict = self._parser.columns_aliases_dict
        filtered_cols = [
            c
            for c in columns_dict.get("select", {})
            if c != "NULL" and not isinstance(c, list)
        ]
        if columns_alias_dict is not None:
            for col_alias in columns_alias_dict.get("select", []):
                if col_alias in self._parser.columns_aliases:
                    col_name = self._parser.columns_aliases[col_alias]
                    filtered_cols = [
                        col_alias if c == col_name else c for c in filtered_cols
                    ]
        # swap back renamed date column
        return ["date" if c == self._DATE_SWAP_TOKEN else c for c in filtered_cols]


class SqlLineageSQLParser(SQLParser):
    _DATE_SWAP_TOKEN = "__d_a_t_e"
    _TIMESTAMP_SWAP_TOKEN = "__t_i_m_e_s_t_a_m_p"
    _MYVIEW_SQL_TABLE_NAME_TOKEN = "__my_view__.__sql_table_name__"
    _MYVIEW_LOOKER_TOKEN = "my_view.SQL_TABLE_NAME"

    def __init__(self, sql_query: str) -> None:
        super().__init__(sql_query)

        original_sql_query = sql_query

        # SqlLineageParser makes mistakes on lateral flatten queries, use the prefix
        if "lateral flatten" in sql_query:
            sql_query = sql_query[: sql_query.find("lateral flatten")]

        # SqlLineageParser makes mistakes on columns called "date", rename them
        sql_query = re.sub(
            r"(\bdate\b)", rf"{self._DATE_SWAP_TOKEN}", sql_query, flags=re.IGNORECASE
        )

        # SqlLineageParser lowercarese tablenames and we need to replace Looker specific token which should be uppercased
        sql_query = re.sub(
            rf"(\${{{self._MYVIEW_LOOKER_TOKEN}}})",
            rf"{self._MYVIEW_SQL_TABLE_NAME_TOKEN}",
            sql_query,
        )

        # SqlLineageParser makes mistakes on columns called "timestamp", rename them
        sql_query = re.sub(
            r"(\btimestamp\b)",
            rf"{self._TIMESTAMP_SWAP_TOKEN}",
            sql_query,
            flags=re.IGNORECASE,
        )

        # SqlLineageParser does not handle "encode" directives well. Remove them
        sql_query = re.sub(r"\sencode [a-zA-Z]*", "", sql_query, flags=re.IGNORECASE)

        # Replace lookml templates with the variable otherwise sqlparse can't parse ${
        sql_query = re.sub(r"(\${)(.+)(})", r"\2", sql_query)
        if sql_query != original_sql_query:
            logger.debug(f"rewrote original query {original_sql_query} as {sql_query}")

        self._sql = sql_query

        self._stmt = [
            s
            for s in sqlparse.parse(
                # first apply sqlparser formatting just to get rid of comments, which cause
                # inconsistencies in parsing output
                sqlparse.format(
                    self._sql.strip(),
                    strip_comments=True,
                    use_space_around_operators=True,
                ),
            )
            if s.token_first(skip_cm=True)
        ]

        with unittest.mock.patch(
            "sqllineage.core.handlers.source.SourceHandler.end_of_query_cleanup",
            datahub.utilities.sqllineage_patch.end_of_query_cleanup_patch,
        ):
            with unittest.mock.patch(
                "sqllineage.core.holders.SubQueryLineageHolder.add_column_lineage",
                datahub.utilities.sqllineage_patch.add_column_lineage_patch,
            ):
                self._stmt_holders = [
                    LineageAnalyzer().analyze(stmt) for stmt in self._stmt
                ]
                self._sql_holder = SQLLineageHolder.of(*self._stmt_holders)

    def get_tables(self) -> List[str]:
        result: List[str] = list()
        for table in self._sql_holder.source_tables:
            table_normalized = re.sub(r"^<default>.", "", str(table))
            result.append(str(table_normalized))

        # We need to revert TOKEN replacements
        result = ["date" if c == self._DATE_SWAP_TOKEN else c for c in result]
        result = [
            "timestamp" if c == self._TIMESTAMP_SWAP_TOKEN else c for c in list(result)
        ]
        result = [
            self._MYVIEW_LOOKER_TOKEN if c == self._MYVIEW_SQL_TABLE_NAME_TOKEN else c
            for c in result
        ]

        # Sort tables to make the list deterministic
        result.sort()

        return result

    def get_columns(self) -> List[str]:
        graph: DiGraph = self._sql_holder.graph  # For mypy attribute checking
        column_nodes = [n for n in graph.nodes if isinstance(n, Column)]
        column_graph = graph.subgraph(column_nodes)

        target_columns = {column for column, deg in column_graph.out_degree if deg == 0}

        result: Set[str] = set()
        for column in target_columns:
            # Let's drop all the count(*) and similard columns which are expression actually if it does not have an alias
            if not any(ele in column.raw_name for ele in ["*", "(", ")"]):
                result.add(str(column.raw_name))

        # Reverting back all the previously renamed words which confuses the parser
        result = set(["date" if c == self._DATE_SWAP_TOKEN else c for c in result])
        result = set(
            [
                "timestamp" if c == self._TIMESTAMP_SWAP_TOKEN else c
                for c in list(result)
            ]
        )
        # swap back renamed date column
        return list(result)


class DefaultSQLParser(SQLParser):
    parser: SQLParser

    def __init__(self, sql_query: str) -> None:
        super().__init__(sql_query)
        self.parser = SqlLineageSQLParser(sql_query)

    def get_tables(self) -> List[str]:
        return self.parser.get_tables()

    def get_columns(self) -> List[str]:
        return self.parser.get_columns()

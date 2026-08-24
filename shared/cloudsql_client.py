"""Cliente Cloud SQL — API estilo Supabase/PostgREST sobre SQLAlchemy.

    get_db().table("contrato").select("*").eq("status", "REGISTRADO").limit(10).execute()
    get_db().table("contrato").insert({...}).execute()
    get_db().table("garantia_ur").upsert({...}, on_conflict="garantia_id, ...").execute()

Sem Django ORM (design §1): DATABASES={} no settings, todo acesso passa por
aqui. Conecta a Postgres local via LOCAL_DATABASE_URL (dev com Docker) ou a
Cloud SQL via Connector (CLOUDSQL_CONNECTION_NAME) — o caminho real usado
nesta máquina, que não tem Docker.
"""

import json
import logging
import os
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class ExecuteResult:
    def __init__(self, data=None, count: Optional[int] = None):
        self.data = data or []
        self.count = count


class QueryBuilder:
    def __init__(self, engine, table_name: str):
        self._engine = engine
        self._table = table_name
        self._select_fields = "*"
        self._count_mode: Optional[str] = None
        self._filters: List[tuple] = []
        self._order_by: List[tuple] = []
        self._limit_val: Optional[int] = None
        self._op = "select"
        self._insert_data = None
        self._update_data: Optional[dict] = None
        self._on_conflict: Optional[str] = None

    def select(self, fields: str = "*", count: Optional[str] = None) -> "QueryBuilder":
        self._select_fields = fields
        self._count_mode = count
        return self

    def eq(self, field: str, value: Any) -> "QueryBuilder":
        self._filters.append(("eq", field, value))
        return self

    def order(self, field: str, desc: bool = False) -> "QueryBuilder":
        self._order_by.append((field, desc))
        return self

    def limit(self, n: int) -> "QueryBuilder":
        self._limit_val = n
        return self

    def insert(self, data) -> "QueryBuilder":
        self._op = "insert"
        self._insert_data = data
        return self

    def upsert(self, data, on_conflict: str) -> "QueryBuilder":
        self._op = "upsert"
        self._insert_data = data
        self._on_conflict = on_conflict
        return self

    def update(self, data: dict) -> "QueryBuilder":
        self._op = "update"
        self._update_data = data
        return self

    def delete(self) -> "QueryBuilder":
        self._op = "delete"
        return self

    def execute(self) -> ExecuteResult:
        try:
            return {
                "select": self._exec_select,
                "insert": self._exec_insert,
                "upsert": self._exec_upsert,
                "update": self._exec_update,
                "delete": self._exec_delete,
            }[self._op]()
        except Exception:
            logger.exception("[CloudSQL] Erro em %s.%s", self._table, self._op)
            raise

    def _build_where(self):
        if not self._filters:
            return "", {}
        clauses, params = [], {}
        for i, (op, field, val) in enumerate(self._filters):
            pname = f"p{i}"
            if op == "eq":
                clauses.append(f"{field} = :{pname}")
                params[pname] = val
        return "WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _serialize(v: Any) -> Any:
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, default=str)
        return v

    @staticmethod
    def _deserialize_row(row: dict) -> dict:
        result = {}
        for k, v in row.items():
            if isinstance(v, str) and len(v) > 1 and v[0] in ("{", "["):
                try:
                    result[k] = json.loads(v)
                    continue
                except (json.JSONDecodeError, ValueError):
                    pass
            result[k] = v
        return result

    def _exec_select(self) -> ExecuteResult:
        from sqlalchemy import text

        where, params = self._build_where()
        with self._engine.connect() as conn:
            if self._count_mode == "exact":
                sql = f"SELECT COUNT(*) FROM {self._table} {where}"
                return ExecuteResult(data=[], count=conn.execute(text(sql), params).scalar())

            order_clause = ""
            if self._order_by:
                parts = [f"{f} {'DESC' if d else 'ASC'}" for f, d in self._order_by]
                order_clause = "ORDER BY " + ", ".join(parts)
            limit_clause = f"LIMIT {self._limit_val}" if self._limit_val else ""

            sql = f"SELECT {self._select_fields} FROM {self._table} {where} {order_clause} {limit_clause}"
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])

    def _exec_insert(self) -> ExecuteResult:
        from sqlalchemy import text

        rows = self._insert_data if isinstance(self._insert_data, list) else [self._insert_data]
        inserted = []
        with self._engine.begin() as conn:
            for row in rows:
                serialized = {k: self._serialize(v) for k, v in row.items()}
                cols = list(serialized.keys())
                placeholders = [f":{c}" for c in cols]
                sql = f"INSERT INTO {self._table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) RETURNING *"
                result = conn.execute(text(sql), serialized)
                inserted.extend(self._deserialize_row(dict(r._mapping)) for r in result)
        return ExecuteResult(data=inserted)

    def _exec_upsert(self) -> ExecuteResult:
        from sqlalchemy import text

        rows = self._insert_data if isinstance(self._insert_data, list) else [self._insert_data]
        upserted = []
        with self._engine.begin() as conn:
            for row in rows:
                serialized = {k: self._serialize(v) for k, v in row.items()}
                cols = list(serialized.keys())
                placeholders = [f":{c}" for c in cols]
                update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
                sql = (
                    f"INSERT INTO {self._table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) "
                    f"ON CONFLICT ({self._on_conflict}) DO UPDATE SET {update_clause} RETURNING *"
                )
                result = conn.execute(text(sql), serialized)
                upserted.extend(self._deserialize_row(dict(r._mapping)) for r in result)
        return ExecuteResult(data=upserted)

    def _exec_update(self) -> ExecuteResult:
        from sqlalchemy import text

        serialized = {k: self._serialize(v) for k, v in self._update_data.items()}
        set_clause = ", ".join(f"{k} = :u_{k}" for k in serialized)
        params = {f"u_{k}": v for k, v in serialized.items()}
        where, where_params = self._build_where()
        params.update(where_params)
        sql = f"UPDATE {self._table} SET {set_clause} {where} RETURNING *"
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])

    def _exec_delete(self) -> ExecuteResult:
        from sqlalchemy import text

        where, params = self._build_where()
        sql = f"DELETE FROM {self._table} {where} RETURNING *"
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])


class CloudSQLClient:
    def __init__(self, engine):
        self._engine = engine

    def table(self, name: str) -> QueryBuilder:
        return QueryBuilder(self._engine, name)


def _create_engine():
    import sqlalchemy

    local_url = os.getenv("LOCAL_DATABASE_URL")
    if local_url:
        logger.info("[CloudSQL] Engine LOCAL via LOCAL_DATABASE_URL")
        return sqlalchemy.create_engine(local_url, pool_pre_ping=True)

    connection_name = os.getenv("CLOUDSQL_CONNECTION_NAME")
    if not connection_name:
        return None

    from google.cloud.sql.connector import Connector, IPTypes

    connector = Connector()
    db_user = os.getenv("CLOUDSQL_DB_USER", "postgres")
    db_pass = os.getenv("CLOUDSQL_DB_PASSWORD", "")
    db_name = os.getenv("CLOUDSQL_DB_NAME", "postgres")
    ip_type = IPTypes[os.getenv("CLOUDSQL_IP_TYPE", "PUBLIC").upper()]

    def getconn():
        return connector.connect(
            connection_name, "pg8000", user=db_user, password=db_pass, db=db_name, ip_type=ip_type,
        )

    logger.info("[CloudSQL] Engine criado para %s (ip_type=%s)", connection_name, ip_type.name)
    return sqlalchemy.create_engine(
        "postgresql+pg8000://", creator=getconn, pool_size=5, max_overflow=2, pool_timeout=30, pool_recycle=1800,
    )


_client: Optional[CloudSQLClient] = None


def get_db() -> Optional[CloudSQLClient]:
    global _client
    if _client is not None:
        return _client
    engine = _create_engine()
    if engine is None:
        return None
    _client = CloudSQLClient(engine)
    return _client

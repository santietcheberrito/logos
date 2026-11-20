import os
import uuid

# Las pruebas corren SIEMPRE contra la base local y con embeddings locales.
os.environ["DATABASE_URL"] = "postgresql://logos@127.0.0.1:55440/logos"
os.environ["EMBEDDINGS_PROVIDER"] = "local"

import pytest  # noqa: E402

from logos import db, gobernanza  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _verificar_base():
    with db.conexion() as conn:
        assert conn.execute("select inet_server_port() p").fetchone()["p"] == 55440
    yield
    db.cerrar()


def _empresa():
    with db.conexion() as conn:
        eid = str(conn.execute("insert into empresas (nombre) values (%s) returning id",
                               (f"Test {uuid.uuid4().hex[:8]}",)).fetchone()["id"])
    admin = gobernanza.identificar(gobernanza.crear_token(eid, "Admin test", "admin"))
    agente = gobernanza.identificar(gobernanza.crear_token(eid, "Agente test", "agente"))
    return eid, admin, agente


@pytest.fixture
def empresa():
    return _empresa()


@pytest.fixture
def otra_empresa():
    return _empresa()

-- =====================================================================
-- Logos — esquema base (multi-tenant, gobernado, versionado)
-- Se aplica tal cual en Supabase (SQL editor o `supabase db push`).
-- =====================================================================

create schema if not exists extensions;
create extension if not exists vector   with schema extensions;
create extension if not exists pg_trgm  with schema extensions;
create extension if not exists pgcrypto with schema extensions;

set search_path = public, extensions;

-- ---------------------------------------------------------------------
-- Empresas (tenants) y tokens de acceso de los agentes / admins
-- ---------------------------------------------------------------------
create table empresas (
  id         uuid primary key default gen_random_uuid(),
  nombre     text not null unique,
  creado_en  timestamptz not null default now()
);

-- Cada agente o admin se conecta al MCP con un token. Solo guardamos el hash.
create table tokens_acceso (
  id          uuid primary key default gen_random_uuid(),
  empresa_id  uuid not null references empresas(id) on delete cascade,
  nombre      text not null,                       -- ej: "Agente de ventas", "Santiago (admin)"
  rol         text not null check (rol in ('agente', 'admin')),
  token_hash  text not null unique,                -- sha256 hex del token
  activo      boolean not null default true,
  creado_en   timestamptz not null default now(),
  ultimo_uso  timestamptz
);

-- ---------------------------------------------------------------------
-- Conocimiento oficial
-- ---------------------------------------------------------------------
create table definiciones (
  id             uuid primary key default gen_random_uuid(),
  empresa_id     uuid not null references empresas(id) on delete cascade,
  termino        text not null,
  definicion     text not null,
  categoria      text,
  dueno          text,
  version        integer not null default 1,
  estado         text not null check (estado in ('borrador', 'aprobado', 'obsoleto')) default 'borrador',
  propuesta_id   uuid,
  creado_en      timestamptz not null default now(),
  actualizado_en timestamptz not null default now(),
  unique (empresa_id, termino, version)
);
-- Nunca dos versiones aprobadas del mismo término
create unique index definiciones_una_aprobada
  on definiciones (empresa_id, lower(termino)) where estado = 'aprobado';
create index definiciones_trgm on definiciones using gin (lower(termino) gin_trgm_ops);

create table procesos (
  id             uuid primary key default gen_random_uuid(),
  empresa_id     uuid not null references empresas(id) on delete cascade,
  nombre         text not null,
  descripcion    text,
  pasos          jsonb not null default '[]'::jsonb,
  responsable    text,
  restricciones  text,
  estado         text not null check (estado in ('borrador', 'aprobado', 'obsoleto')) default 'borrador',
  version        integer not null default 1,
  propuesta_id   uuid,
  creado_en      timestamptz not null default now(),
  actualizado_en timestamptz not null default now(),
  unique (empresa_id, nombre, version)
);
create unique index procesos_uno_aprobado
  on procesos (empresa_id, lower(nombre)) where estado = 'aprobado';
create index procesos_trgm on procesos using gin (lower(nombre) gin_trgm_ops);

create table reglas (
  id             uuid primary key default gen_random_uuid(),
  empresa_id     uuid not null references empresas(id) on delete cascade,
  nombre         text not null,
  descripcion    text,
  aplica_a       text not null,                    -- "agentes", "ventas", "soporte", "todos"...
  restriccion    text not null,
  estado         text not null check (estado in ('borrador', 'aprobado', 'obsoleto')) default 'borrador',
  version        integer not null default 1,
  propuesta_id   uuid,
  creado_en      timestamptz not null default now(),
  actualizado_en timestamptz not null default now(),
  unique (empresa_id, nombre, version)
);
create unique index reglas_una_aprobada
  on reglas (empresa_id, lower(nombre)) where estado = 'aprobado';
create index reglas_aplica_a on reglas (empresa_id, lower(aplica_a)) where estado = 'aprobado';

-- ---------------------------------------------------------------------
-- Documentos y chunks (búsqueda semántica + texto completo)
-- ---------------------------------------------------------------------
create table documentos (
  id             uuid primary key default gen_random_uuid(),
  empresa_id     uuid not null references empresas(id) on delete cascade,
  titulo         text not null,
  contenido      text not null,
  tipo           text,                              -- SOP, política, FAQ, manual...
  fuente         text,
  version        integer not null default 1,
  estado         text not null check (estado in ('borrador', 'aprobado', 'obsoleto')) default 'borrador',
  metadata       jsonb not null default '{}'::jsonb,
  creado_en      timestamptz not null default now(),
  actualizado_en timestamptz not null default now()
);
create unique index documentos_uno_aprobado
  on documentos (empresa_id, lower(titulo)) where estado = 'aprobado';

create table chunks (
  id            uuid primary key default gen_random_uuid(),
  empresa_id    uuid not null references empresas(id) on delete cascade,
  documento_id  uuid not null references documentos(id) on delete cascade,
  orden         integer not null,
  contenido     text not null,
  embedding     extensions.vector(1536),           -- text-embedding-3-small
  tsv           tsvector generated always as (to_tsvector('spanish', contenido)) stored,
  metadata      jsonb not null default '{}'::jsonb,
  creado_en     timestamptz not null default now()
);
create index chunks_embedding on chunks using hnsw (embedding extensions.vector_cosine_ops);
create index chunks_tsv on chunks using gin (tsv);
create index chunks_documento on chunks (documento_id);

-- ---------------------------------------------------------------------
-- Gobernanza: propuestas de cambio y auditoría
-- ---------------------------------------------------------------------
create table propuestas (
  id              uuid primary key default gen_random_uuid(),
  empresa_id      uuid not null references empresas(id) on delete cascade,
  tipo            text not null check (tipo in ('definicion', 'proceso', 'regla', 'documento')),
  contenido       jsonb not null,
  justificacion   text,
  propuesto_por   text,
  estado          text not null check (estado in ('pendiente', 'aprobada', 'rechazada')) default 'pendiente',
  revisado_por    text,
  motivo_revision text,
  resultado_id    uuid,                             -- registro creado al aprobar
  creado_en       timestamptz not null default now(),
  revisado_en     timestamptz
);
create index propuestas_pendientes on propuestas (empresa_id, creado_en) where estado = 'pendiente';

-- Log append-only de todo lo que pasa por el MCP y el dashboard
create table auditoria (
  id          bigint generated always as identity primary key,
  empresa_id  uuid references empresas(id) on delete cascade,
  actor       text not null,
  rol         text,
  accion      text not null,
  detalle     jsonb not null default '{}'::jsonb,
  creado_en   timestamptz not null default now()
);
create index auditoria_empresa_fecha on auditoria (empresa_id, creado_en desc);

-- La auditoría no se edita ni se borra (ni siquiera desde el backend)
create function auditoria_inmutable() returns trigger language plpgsql as $$
begin
  raise exception 'La auditoría es de solo agregado';
end $$;
create trigger auditoria_sin_update before update or delete on auditoria
  for each row execute function auditoria_inmutable();

-- ---------------------------------------------------------------------
-- Row Level Security
-- El MCP Server y el dashboard usan la conexión de backend (rol postgres /
-- service_role), que salta RLS. Estas políticas protegen cualquier acceso
-- directo con la anon key o con un usuario de Supabase Auth.
-- Un usuario autenticado lleva en app_metadata: {"empresa_id": "...", "rol": "agente|admin"}
-- ---------------------------------------------------------------------
create function logos_empresa_id() returns uuid
  language sql stable security definer set search_path = public as $$
  select nullif(coalesce(auth.jwt() -> 'app_metadata' ->> 'empresa_id', ''), '')::uuid
$$;

create function logos_es_admin() returns boolean
  language sql stable security definer set search_path = public as $$
  select coalesce(auth.jwt() -> 'app_metadata' ->> 'rol', '') = 'admin'
$$;

alter table empresas      enable row level security;
alter table tokens_acceso enable row level security;
alter table definiciones  enable row level security;
alter table procesos      enable row level security;
alter table reglas        enable row level security;
alter table documentos    enable row level security;
alter table chunks        enable row level security;
alter table propuestas    enable row level security;
alter table auditoria     enable row level security;

-- service_role: acceso total (backend)
do $$
declare t text;
begin
  foreach t in array array['empresas','tokens_acceso','definiciones','procesos','reglas',
                           'documentos','chunks','propuestas','auditoria']
  loop
    execute format('create policy %I on %I for all to service_role using (true) with check (true)',
                   t || '_service_role', t);
  end loop;
end $$;

-- empresas: cada usuario ve solo la suya
create policy empresas_propia on empresas for select to authenticated
  using (id = logos_empresa_id());

-- tokens_acceso: sin política para authenticated = invisible (solo backend)

-- Conocimiento: autenticados leen lo APROBADO de su empresa; admins leen todo lo de su empresa.
-- Nadie autenticado escribe directo: los cambios entran solo por propuestas.
do $$
declare t text;
begin
  foreach t in array array['definiciones','procesos','reglas','documentos']
  loop
    execute format($f$
      create policy %I on %I for select to authenticated
      using (empresa_id = logos_empresa_id() and (estado = 'aprobado' or logos_es_admin()))
    $f$, t || '_lectura', t);
  end loop;
end $$;

create policy chunks_lectura on chunks for select to authenticated
  using (
    empresa_id = logos_empresa_id()
    and exists (select 1 from documentos d
                where d.id = chunks.documento_id
                  and (d.estado = 'aprobado' or logos_es_admin()))
  );

-- Propuestas: cualquier autenticado de la empresa puede proponer (queda pendiente);
-- solo un admin las ve todas y las revisa.
create policy propuestas_crear on propuestas for insert to authenticated
  with check (empresa_id = logos_empresa_id() and estado = 'pendiente'
              and revisado_por is null and revisado_en is null and resultado_id is null);
create policy propuestas_ver on propuestas for select to authenticated
  using (empresa_id = logos_empresa_id() and logos_es_admin());

-- Auditoría: solo admins de la empresa la leen
create policy auditoria_ver on auditoria for select to authenticated
  using (empresa_id = logos_empresa_id() and logos_es_admin());

-- anon: sin ninguna política => sin acceso a nada.

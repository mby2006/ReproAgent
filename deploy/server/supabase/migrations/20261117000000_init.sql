-- ReproAgent core schema.
-- Ownership root is projects.user_id; every other table derives ownership
-- transitively through its project/paper/experiment/run foreign key. RLS is
-- enforced at the database, never relying on the frontend to hide rows.

create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ---------- projects (ownership root) ----------
create table public.projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  name text not null check (char_length(name) between 1 and 200),
  description text,
  is_default boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index projects_user_id_idx on public.projects (user_id);
-- At most one default project per user; the upload flow get-or-creates it.
create unique index projects_one_default_per_user
  on public.projects (user_id) where is_default;

create trigger projects_set_updated_at
  before update on public.projects
  for each row execute function public.set_updated_at();

-- ---------- papers ----------
create table public.papers (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects (id) on delete cascade,
  title text not null check (char_length(title) between 1 and 500),
  doi text,
  source text not null default 'upload' check (source in ('upload', 'url')),
  source_url text,
  pdf_path text not null,
  file_sha256 text,
  metadata_json jsonb not null default '{}'::jsonb,
  status text not null default 'uploaded' check (status in ('uploaded', 'parsed', 'failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (source <> 'url' or source_url is not null)
);

create index papers_project_id_idx on public.papers (project_id);
create index papers_file_sha256_idx on public.papers (file_sha256);

create trigger papers_set_updated_at
  before update on public.papers
  for each row execute function public.set_updated_at();

-- ---------- reproduction_specs: append-only card versions ----------
create table public.reproduction_specs (
  id uuid primary key default gen_random_uuid(),
  paper_id uuid not null references public.papers (id) on delete cascade,
  version integer not null check (version >= 1),
  spec_json jsonb not null,
  origin text not null check (origin in ('extraction', 'user_edit')),
  parent_version_id uuid references public.reproduction_specs (id),
  status text not null default 'draft' check (status in ('draft', 'confirmed', 'superseded')),
  created_at timestamptz not null default now(),
  unique (paper_id, version)
);

create index reproduction_specs_paper_version_idx
  on public.reproduction_specs (paper_id, version desc);

-- ---------- understanding_jobs: async parse jobs ----------
create table public.understanding_jobs (
  id uuid primary key default gen_random_uuid(),
  paper_id uuid not null references public.papers (id) on delete cascade,
  status text not null default 'queued'
    check (status in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  spec_version_id uuid references public.reproduction_specs (id),
  document jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index understanding_jobs_paper_id_idx on public.understanding_jobs (paper_id);

create trigger understanding_jobs_set_updated_at
  before update on public.understanding_jobs
  for each row execute function public.set_updated_at();

-- ---------- ownership helpers (security invoker: RLS still applies) ----------
-- Defined after their referenced tables exist (SQL functions validate at create
-- time). Each terminates at projects, so the call graph is acyclic.

create or replace function public.owns_project(p_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.projects where id = p_id and user_id = auth.uid()
  )
$$;

create or replace function public.owns_paper(pa_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.papers pa
    where pa.id = pa_id and public.owns_project(pa.project_id)
  )
$$;

-- ---------- Row Level Security ----------
alter table public.projects enable row level security;
alter table public.papers enable row level security;
alter table public.reproduction_specs enable row level security;
alter table public.understanding_jobs enable row level security;

create policy projects_owner_select on public.projects
  for select using (auth.uid() = user_id);
create policy projects_owner_insert on public.projects
  for insert with check (auth.uid() = user_id);
create policy projects_owner_update on public.projects
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy projects_owner_delete on public.projects
  for delete using (auth.uid() = user_id);

create policy papers_owner_select on public.papers
  for select using (public.owns_project(project_id));
create policy papers_owner_insert on public.papers
  for insert with check (public.owns_project(project_id));
create policy papers_owner_update on public.papers
  for update using (public.owns_project(project_id)) with check (public.owns_project(project_id));
create policy papers_owner_delete on public.papers
  for delete using (public.owns_project(project_id));

-- Append-only: no update/delete policies exist, so even the owner cannot
-- mutate or remove an old card version through the API.
create policy specs_owner_select on public.reproduction_specs
  for select using (public.owns_paper(paper_id));
create policy specs_owner_insert on public.reproduction_specs
  for insert with check (public.owns_paper(paper_id));

create policy ujobs_owner_select on public.understanding_jobs
  for select using (public.owns_paper(paper_id));
create policy ujobs_owner_insert on public.understanding_jobs
  for insert with check (public.owns_paper(paper_id));
create policy ujobs_owner_update on public.understanding_jobs
  for update using (public.owns_paper(paper_id)) with check (public.owns_paper(paper_id));
create policy ujobs_owner_delete on public.understanding_jobs
  for delete using (public.owns_paper(paper_id));

-- ---------- Storage: private per-user paper bucket ----------
insert into storage.buckets (id, name, public)
values ('papers', 'papers', false)
on conflict (id) do nothing;

-- Object path convention: {user_id}/{paper_id}.pdf
create policy papers_storage_owner_select on storage.objects
  for select using (
    bucket_id = 'papers' and (storage.foldername(name))[1] = auth.uid()::text
  );
create policy papers_storage_owner_insert on storage.objects
  for insert with check (
    bucket_id = 'papers' and (storage.foldername(name))[1] = auth.uid()::text
  );
create policy papers_storage_owner_update on storage.objects
  for update using (
    bucket_id = 'papers' and (storage.foldername(name))[1] = auth.uid()::text
  );
create policy papers_storage_owner_delete on storage.objects
  for delete using (
    bucket_id = 'papers' and (storage.foldername(name))[1] = auth.uid()::text
  );
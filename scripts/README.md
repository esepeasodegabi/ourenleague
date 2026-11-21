# Scripts de la liga y de la base de datos

Copia de los scripts que actualizan la base (schema `h4c` en Supabase) y calculan y publican la liga Ourenleague.

Estos archivos están aquí para versionarlos. En el día a día se ejecutan en el entorno de trabajo (`/workspace/h4c` y `/workspace/liga`), no desde esta carpeta del repositorio. La publicación de la web conserva `scripts/` y solo regenera `index.html`, `data.json`, `img/` y `.nojekyll`.

No hace falta ninguna credencial para leer este código. Los secretos no van en el repositorio: se leen de variables de entorno o de archivos locales que no se suben (por ejemplo `state/pg.env`).

## Qué hace cada archivo

- **extract.py** — Extrae del portal My Hit4Change (solo lectura) e incorpora los datos a Postgres, schema `h4c`. Admite una carga completa o una ventana reciente (`--recent`). Necesita los módulos locales `h4c_client.py` y `staging_store.py`, el certificado de Supabase y un entorno con `psycopg`.
- **run.sh** — Arranca `extract.py` con el entorno virtual (`.venv`) que está junto al script.
- **liga.py** — Calcula la puntuación de la liga desde una fecha de inicio y genera la web estática. Es de solo lectura: no escribe ni modifica la base de datos. Para la regla I❤️BF consulta el portal, perfil a perfil. También importa `connect_postgres` de `extract.py` y el cliente `h4c_client.py`.
- **publish.sh** — Regenera la web con `liga.py` (salvo `--skip-build`) y la publica en la rama `main` de este repositorio, para GitHub Pages. Conserva `scripts/`. El token de GitHub se pasa por `GIT_ASKPASS`; nunca se escribe en la URL del remote ni en la config de git.
- **post_class_tick.sh** / **post_class_tick.py** — Pensados para invocarse cada ~15 minutos. Cuando ha pasado una hora desde el inicio de una clase (según las sesiones del portal), ejecutan `extract.py --recent` y después `publish.sh`.
- **config.json** — Fecha de inicio de la liga, título, repositorio y carpeta de salida de la web. No contiene datos personales.

## Variables de entorno (solo los nombres)

No copies aquí los valores. Si faltan, los scripts fallan o omiten la parte que las necesita.

Para extraer y para calcular la liga:

- `H4C_EMAIL` — usuario del portal My Hit4Change
- `H4C_PASSWORD` — contraseña del portal
- `SUPABASE_DB_URL` — URI de Postgres (el usuario y la contraseña van en la propia URI, o se pueden sustituir con las dos siguientes)
- `SUPABASE_DB_USER` — rol de Postgres (opcional; en el entorno de trabajo se carga desde un archivo local)
- `SUPABASE_DB_PASSWORD` — contraseña de ese rol (opcional; igual que la anterior)
- `SUPABASE_REGION` — región del pooler de Supabase (opcional)
- `H4C_PG_SSLMODE` / `H4C_PG_DEADLINE` — ajustes opcionales de la conexión
- `PGPASSWORD` — alternativa estándar de libpq (opcional)
- `SUPABASE_CA_FILE` — ruta a un certificado CA (opcional)

Para publicar la web:

- `GITHUB_OURENLEAGUE_TOKEN` — token de GitHub con permiso de escritura en este repositorio y, si se configura Pages desde el script, permisos de Pages

`post_class_tick` necesita el conjunto de las anteriores, porque lanza la extracción y la publicación.

## Qué no está en esta carpeta

A propósito no se suben `state/`, logs, bases SQLite de staging, archivos `.env`, certificados, el entorno virtual, ni los módulos de apoyo (`h4c_client.py`, `staging_store.py`, `git-askpass.sh`, `github_pages.py`, `scan_site.py`, `template.html`). Sin ellos, y sin las variables de entorno, estos scripts no se pueden ejecutar tal cual desde una copia del repo.

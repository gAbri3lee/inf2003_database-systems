"""Launch Streamlit, starting an existing local PostgreSQL cluster if needed."""
import sys,os,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import config
import psycopg

def main():
    try:
        with psycopg.connect(config.pg_dsn(),connect_timeout=3): pass
    except psycopg.OperationalError:
        data=Path(os.environ.get('PGDATA',config.PROJECT_DIR/'pgdata'))
        binary=config.PROJECT_DIR/'vendor/pgsql/bin/pg_ctl.exe'
        if config.PG_HOST not in ('localhost','127.0.0.1') or not binary.exists() or not (data/'PG_VERSION').exists():
            raise SystemExit('PostgreSQL unavailable. Start the configured database or follow README setup instructions.')
        subprocess.run([str(binary),'-D',str(data),'-l',str(data/'server.log'),'-o',f'-p {config.PG_PORT} -h 127.0.0.1','-w','start'],check=True)
    return subprocess.call([sys.executable,'-m','streamlit','run',str(config.PROJECT_DIR/'app.py')],cwd=config.PROJECT_DIR)

if __name__=='__main__': raise SystemExit(main())

# Em dev, lê server/.env. Na VPS o arquivo não existe e as variáveis vêm do
# env_file do docker-compose (load_dotenv não sobrescreve o que já existe).
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

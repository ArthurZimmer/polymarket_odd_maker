# poly-scraper

Bot que opera a **Polymarket como market maker** explorando o delay estrutural do mercado peer-to-peer em eventos esportivos pré-live. Compara odds da Polymarket contra um consenso ponderado das casas tradicionais (Pinnacle, bet365, Betano, Superbet, Estrela Bet) e entra quando o EV é positivo.

> **Status:** em desenvolvimento ativo. Plano completo em `~/.claude/plans/ultraplan-cannot-launch-remote-typed-flame.md`.

## Arquitetura

```
Frontend (Next.js, :3000) ── HTTP/WS ──► Backend FastAPI (:8000, asyncio)
                                          ├── PolymarketWatcher  (WS preços)
                                          ├── Bookmaker Scrapers (5 casas)
                                          ├── Event Matcher      (fuzzy match)
                                          ├── EV Engine          (devig + consensus)
                                          ├── Trading Engine     (Polymarket CLOB)
                                          └── Position Manager   (lifecycle + saída)
                                          ↕
                                       SQLite (~/.poly-scraper/db.sqlite)
```

## Setup local

```bash
# 1) Backend — Python 3.12+
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2) Frontend
cd frontend && npm install && cd ..

# 3) Iniciar tudo
./dev.sh
```

Verificar: <http://localhost:3000> (UI) e <http://localhost:8000/health> (API).

## Configuração

Variáveis de ambiente em `.env` (ver `.env.example`). Credenciais sensíveis (private key Polymarket) são injetadas pelo dashboard e criptografadas com Fernet em `~/.poly-scraper/config.encrypted`.

## Aviso legal

Operação automatizada de apostas e trading on-chain envolve risco financeiro real. Este software não oferece garantia de lucro. Use por sua conta e risco.

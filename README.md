# Mandaxpress

PDV (frente de caixa) simples para eventos, derivado conceitualmente do
Mandaladaka. Primeiro uso: **Festival do Açaí — 31/10/2026**.

> **Estado: Bloco 1 (fundação) concluído. O projeto NÃO está pronto para uso
> real.** Há login, sessão, perfis, banco, Docker e testes. Venda rápida,
> comandas, créditos, estornos e fechamento **ainda não existem**. Não use com
> dinheiro real nem exponha na internet nesta etapa. Nenhum deploy foi feito.
> O modelo financeiro proposto para os próximos blocos está em
> [docs/financial-model.md](docs/financial-model.md).

## Stack

Next.js 15 / React 19 / TypeScript · Flask + Gunicorn · MariaDB 11 · Caddy
(proxy reverso) · Docker Compose.

```text
Mandaxpress/
├── backend/            # Flask (domain / use_cases / adapters), testes, Dockerfile
├── frontend/           # Next.js (app, components, lib), Dockerfile
├── database/
│   ├── migrations/     # SQL versionado (001_..., 002_...)
│   └── dev-init/       # cria o banco de testes (somente dev)
├── scripts/            # init-env, backup, restore
├── docker-compose.yml          # base (segura: banco sem porta publicada)
├── docker-compose.dev.yml      # dev: banco em 127.0.0.1, serviço de testes
├── docker-compose.prod.yml     # prod: proxy em 80/443
├── Caddyfile
├── .env.example / .env.production.example
└── NOTICE.md
```

Frontend (`/`) e API (`/api`) ficam na **mesma origem** atrás do Caddy; por isso
o cookie de sessão é `HttpOnly` + `SameSite=Strict` e não há CORS.

## Requisitos

Docker com Compose v2. Nada mais precisa ser instalado na máquina.

## Instalação e execução (desenvolvimento)

```bash
git clone <repositório> && cd Mandaxpress
./scripts/init-env.sh dev          # cria .env com senhas aleatórias (não versionado)
docker compose up -d --build       # sobe db, migrate, backend, frontend, proxy
docker compose ps                  # todos devem ficar "healthy"/"Exited (0)" (migrate)
```

Acesse **http://localhost:8080** (altere `HTTP_PORT` e `ALLOWED_ORIGINS` no
`.env` se a porta estiver ocupada; os dois devem concordar).

As migrations são aplicadas automaticamente pelo serviço `migrate` (tarefa
única) antes do backend iniciar. Comandos úteis:

```bash
docker compose run --rm migrate python -m src.cli migrate-status
```

### Criar o primeiro administrador

Não existe usuário nem senha padrão. A senha é pedida sem eco:

```bash
docker compose exec backend python -m src.cli create-user \
  --username ana --name "Ana Silva" --role admin
```

Crie operadores com `--role operator`. Redefinir senha (revoga as sessões):
`python -m src.cli set-password --username ana`.
Senha: mínimo 10 caracteres, máximo 72 bytes.
Modo não interativo: `printf '%s\n' "$SENHA" | ... --password-stdin`.

### Testes automatizados

```bash
docker compose run --rm test     # pytest contra um MariaDB real (banco *_test isolado)
cd frontend && npm ci && npm run typecheck && npm run build
```

O banco de testes é criado só quando o volume do banco é criado pela primeira
vez (`database/dev-init`). Em um volume antigo, crie-o manualmente:
`CREATE DATABASE mandaxpress_test` e conceda privilégios ao usuário da aplicação.

## Dev × produção

| | Dev | Produção |
|---|---|---|
| Modelo de `.env` | `.env.example` | `.env.production.example` |
| `COMPOSE_FILE` | base + `dev` | base + `prod` |
| Banco | `127.0.0.1:3307` | **nenhuma porta publicada** |
| Cookie `Secure` | opcional | **obrigatório** (backend recusa iniciar sem) |
| Origens | http local | https obrigatório |
| Servidor | Gunicorn | Gunicorn (nunca `debug`) |

```bash
./scripts/init-env.sh prod   # depois edite SITE_ADDRESS e ALLOWED_ORIGINS
docker compose up -d --build
```

Em produção o backend **recusa iniciar** com `COOKIE_SECURE=false`, senha de
banco `CHANGE_ME` ou origens `http://` (exceto localhost). O projeto não está
acoplado a nenhum provedor: qualquer host que rode Docker Compose serve. O
HTTPS é resolvido pelo Caddy (domínio em `SITE_ADDRESS`) ou pela plataforma
(`SITE_ADDRESS=:80` atrás do proxy dela).

## Persistência, backup e restauração

Os dados ficam no volume Docker nomeado `mandaxpress_db_data`: sobrevivem a
`docker compose down`, `restart` e rebuild. **`docker compose down -v` apaga o
banco** — nunca use `-v` com dados reais.

```bash
./scripts/backup.sh                          # gera backups/mandaxpress-AAAAMMDD-HHMMSS.sql.gz
./scripts/restore.sh backups/ARQUIVO.sql.gz  # pede confirmação e faz backup de segurança antes
```

Os backups têm dados financeiros: copie-os para fora do servidor e do
repositório (a pasta `backups/` é ignorada pelo Git). Teste a restauração
antes do evento.

## Hospedagem gratuita (opções e limitações)

Nada foi contratado nem publicado. O projeto não depende de provedor; os dados
financeiros exigem disco persistente e MariaDB. Condições de planos gratuitos
mudam: confirme antes de decidir.

| Opção | Roda esta stack? | Limitações |
|---|---|---|
| Oracle Cloud Always Free (VM) | Sim, Docker Compose completo | Aprovação da conta é difícil e pede cartão; instâncias ociosas podem ser recuperadas; ARM (`arm64`), ainda não testado aqui |
| Computador próprio + túnel (ex.: Cloudflare Tunnel) | Sim, o mesmo Compose | Depende do PC e da internet ligados durante o evento; fazer backups |
| Fly.io | Não grátis | Sem plano gratuito (apenas teste curto); não executa Compose |
| Render | Não serve | Sem Compose; sem MariaDB gerenciado grátis; serviços dormem após inatividade |

Plataformas com disco efêmero ou bancos gratuitos que expiram **não** servem
para dados financeiros.

## Segurança (resumo do Bloco 1)

- Sessão no servidor: cookie opaco; o banco guarda só o SHA-256 do token.
  Expira por inatividade e por limite absoluto; logout revoga de verdade.
- Senhas com bcrypt; mensagens de login idênticas para usuário inexistente,
  senha errada ou conta inativa.
- CSRF: verificação de `Origin`/`Sec-Fetch-Site` + token `X-CSRF-Token` por
  sessão em toda requisição que altera dados. O token vive só em memória.
- Autorização por perfil (`admin`, `operator`) sempre no backend.
- Erros internos retornam um código de requisição, nunca stack trace.
- Nenhum segredo no repositório; `.env` é ignorado pelo Git.

## Licença e atribuição

Derivado conceitualmente do projeto acadêmico **Mandaladaka** (licença MIT).
O aviso de copyright e a atribuição estão em [NOTICE.md](NOTICE.md) e devem ser
preservados ao reutilizar qualquer trecho. Imagens e ícones sem origem
confirmada não foram reutilizados. Este repositório ainda não define licença
própria.

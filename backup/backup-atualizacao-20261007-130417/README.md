# Gestão TI — Prefeitura Municipal de Santa Luzia

Central de gestão da equipe de Tecnologia da Informação da Prefeitura de Santa Luzia/MG.
Reúne, numa única aplicação web com login pelo Active Directory, as ferramentas do dia a dia da TI:
liberação do Wi-Fi nos computadores, senhas dos visitantes do Wi-Fi, gestão das contas do AD,
disparo de e-mails em lote, configurações e um registro completo de tudo o que é feito.

Este sistema substitui o antigo **pmsl-wifi-aprovacao**: o módulo *Wi-Fi no computador* é a evolução
dele, e o script `migrar_de_wifiaprovacao.sh` faz a troca no servidor copiando todos os dados.

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Tecnologias utilizadas](#2-tecnologias-utilizadas)
3. [Arquitetura](#3-arquitetura)
4. [Estrutura de pastas](#4-estrutura-de-pastas)
5. [Módulos](#5-módulos)
6. [Acesso, permissões e adm geral](#6-acesso-permissões-e-adm-geral)
7. [Gestão de Usuários](#7-gestão-de-usuários)
8. [Rede Sem Fio → Wi-Fi no computador](#8-rede-sem-fio--wi-fi-no-computador)
9. [Rede Sem Fio → Wi-Fi Visitante](#9-rede-sem-fio--wi-fi-visitante)
10. [E-mails em lote](#10-e-mails-em-lote)
11. [Configurações](#11-configurações)
12. [Logs e auditoria](#12-logs-e-auditoria)
13. [Active Directory](#13-active-directory)
14. [Variáveis do `.env`](#14-variáveis-do-env)
15. [Instalação local (testes)](#15-instalação-local-testes)
16. [Subida no servidor (troca do sistema antigo)](#16-subida-no-servidor-troca-do-sistema-antigo)
17. [Atualização, backup e restauração](#17-atualização-backup-e-restauração)
18. [Endereço gestaoti e proxy reverso](#18-endereço-gestaoti-e-proxy-reverso)
19. [Estações Windows (GPO)](#19-estações-windows-gpo)
20. [Endereços das telas e da API](#20-endereços-das-telas-e-da-api)
21. [Banco de dados](#21-banco-de-dados)
22. [Segurança](#22-segurança)
23. [Solução de problemas](#23-solução-de-problemas)
24. [Comandos úteis](#24-comandos-úteis)

---

## 1. Visão geral

- **Uma entrada só:** todos acessam com o usuário e a senha do AD. Há também um usuário local (`admin`)
  para emergência, caso o AD esteja fora do ar.
- **Módulos por pessoa:** cada usuário vê só os módulos que recebeu. Quem não tem permissão vê os
  módulos de e-mail marcados como *Aguardando liberação*.
- **Tudo registrado:** cada ação (login, permissão, senha, liberação, disparo, configuração) vai para o
  módulo **Logs**, com autor, IP, data e detalhe. Senhas nunca são gravadas.
- **Configuração pela tela:** servidor de e-mail e banco do Wi-Fi Visitante são configurados no módulo
  **Configurações**, com os segredos criptografados no banco.
- **Roda em Docker:** dois containers (aplicação e PostgreSQL) e um terceiro opcional para testes de
  e-mail (Mailpit).

---

## 2. Tecnologias utilizadas

### Backend

| Tecnologia | Versão | Uso |
| --- | --- | --- |
| **Python** | 3.12 (imagem `python:3.12-slim`) | Linguagem da aplicação |
| **FastAPI** | 0.115.6 | Framework web: rotas da API, validação, dependências de permissão |
| **Uvicorn** (`uvicorn[standard]`) | 0.34.0 | Servidor ASGI que executa o FastAPI (com `--proxy-headers`) |
| **Pydantic** | (dependência do FastAPI) | Validação dos dados recebidos pela API |
| **SQLAlchemy** | 2.0.36 | ORM: modelos, consultas e migrações simples de colunas |
| **psycopg** (`psycopg[binary]`) | 3.2.3 | Driver do PostgreSQL |
| **ldap3** | 2.9.1 | Comunicação com o Active Directory: login (bind), buscas paginadas, troca de senha, desbloqueio e reativação |
| **dnspython** | 2.7.0 | DNS reverso (PTR) para identificar o computador pelo IP |
| **bcrypt** | 4.2.1 | Hash das senhas dos usuários locais |
| **itsdangerous** | 2.2.0 | Assinatura dos cookies de sessão |
| **cryptography** (Fernet) | 44.0.0 | Criptografia dos segredos salvos pela tela (senha do SMTP, do banco e do SSH do RADIUS) |
| **httpx** | 0.28.1 | Chamadas HTTP para Telegram e WhatsApp (Evolution API) |
| **PyMySQL** | 1.1.1 | Conexão com o banco MySQL/MariaDB do portal de visitantes (RADIUS) |
| **sshtunnel** + **paramiko** | 0.4.0 / 3.5.1 | Túnel SSH até o servidor do RADIUS quando o MySQL só aceita conexões locais |
| **python-multipart** | 0.0.20 | Upload de anexos, imagens e listas TXT |
| **smtplib / email** | biblioteca padrão | Envio dos e-mails (STARTTLS, SSL ou sem criptografia) |
| **threading** | biblioteca padrão | Fila de envio de e-mails e carga do AD em segundo plano |

### Banco de dados

| Tecnologia | Versão | Uso |
| --- | --- | --- |
| **PostgreSQL** | 16 (imagem `postgres:16-alpine`) | Banco da Gestão TI |
| **MySQL / MariaDB** | o do portal | Banco `radio` do RADIUS (somente leitura e `UPDATE` nas tabelas de visitantes) |

### Frontend

| Tecnologia | Uso |
| --- | --- |
| **HTML5 + CSS3 + JavaScript puro** (sem framework e sem etapa de build) | Todas as telas, servidas pelo próprio FastAPI em `api/app/static` |
| **SweetAlert2** (arquivo local `sweetalert2.all.min.js`) | Janelas de confirmação, formulários e avisos (não usamos `window.confirm`) |
| **Fetch API + AbortController** | Chamadas à API e cancelamento de buscas anteriores ao digitar |
| **SVG** | Ícones e favicon embutidos |

### Infraestrutura e scripts

| Tecnologia | Uso |
| --- | --- |
| **Docker** + **Docker Compose** (plugin `docker compose`) | Execução dos containers `gestao-ti-api`, `gestao-ti-db` e `gestao-ti-mailpit` |
| **Mailpit** (`axllent/mailpit`) | Servidor de e-mail falso para testes (perfil `teste`) |
| **Bash** | `aplicar_gestao_ti.sh` (instala/atualiza) e `migrar_de_wifiaprovacao.sh` (troca o sistema antigo) |
| **PowerShell** | `wlan-filter.ps1`, executado nas estações pela GPO (bloqueio/liberação do Wi-Fi) |
| **Active Directory / GPO / NETLOGON** | Autenticação, grupos/OUs, distribuição do script e do atalho do portal |
| **FreeRADIUS (banco `radio`)** | Autenticação dos visitantes no Wi-Fi; a Gestão TI altera as senhas direto nas tabelas dele |

---

## 3. Arquitetura

```
                ┌───────────────────────────┐
 Navegador ───► │  gestao-ti-api (FastAPI)  │ ◄─── Estações Windows (wlan-filter.ps1, a cada 5 min)
                │  porta 8090 → 8000        │       GET /api/agente/liberacao  (X-Agent-Key)
                └──┬────────┬────────┬──────┘
                   │        │        │
       PostgreSQL ◄┘        │        └► MySQL do portal de visitantes (10.1.0.4, banco radio)
     (gestao-ti-db)         │             direto ou por túnel SSH
                            │
            AD 10.1.100.2 / 10.1.100.3 (LDAPS 636 / StartTLS 389, failover)
                            │
                  SMTP (conta geral@)  ·  Telegram / WhatsApp (avisos do Wi-Fi)
```

- **Uma única aplicação** serve as telas (HTML estático) e a API (`/api/...`).
- **Processos em segundo plano** dentro da API:
  - fila de envio de e-mails (retoma sozinha depois de reiniciar);
  - bloqueio automático das liberações do Wi-Fi que venceram;
  - carga da lista completa do AD na subida e atualização periódica (cache em memória).
- **Middlewares:** compressão GZip (respostas acima de 2 KB) e registro do IP de cada requisição para os logs.
- **Dados persistentes** em dois volumes Docker: `dados_db` (PostgreSQL) e `dados_app` (anexos e imagens dos e-mails).

---

## 4. Estrutura de pastas

```
gestao-ti/
├── .env.example                  modelo de configuração (copiado para .env na 1ª instalação)
├── .env                          configuração real (NÃO versionar; permissão 600)
├── docker-compose.yml            serviços db, api e mailpit (perfil "teste")
├── aplicar_gestao_ti.sh          instala ou atualiza em /opt/gestao-ti
├── migrar_de_wifiaprovacao.sh    troca o sistema antigo do Wi-Fi pela Gestão TI, copiando os dados
├── marca/                        coloque logo.svg ou logo.png aqui (aparece em todas as telas)
├── cliente-windows/
│   ├── wlan-filter.ps1           modelo do script da GPO (endereço e chave preenchidos na instalação)
│   └── icone/                    ícone do atalho do portal
├── saida-windows/                (gerada na instalação) script da GPO pronto para o NETLOGON
└── api/
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── main.py               aplicação FastAPI: login, portal Wi-Fi, painel Wi-Fi, usuários, páginas
        ├── config.py             leitura do .env
        ├── db.py                 modelos SQLAlchemy e migrações de colunas
        ├── auth.py               sessão, adm geral, exigir_modulo, quem pode conceder
        ├── modulos.py            catálogo de módulos e grupos
        ├── ad.py                 conexão com o AD (LDAPS/StartTLS, failover entre DCs) — `python -m app.ad` testa
        ├── ad_senhas.py          lista do AD em cache, redefinir senha, desbloquear, reativar (regra do SEI)
        ├── senhas_api.py         rotas /api/senhas
        ├── visitante_api.py      rotas /api/visitante (Wi-Fi Visitante)
        ├── radius.py             acesso ao banco do RADIUS (MySQL), túnel SSH, formatos de senha
        ├── email_lote.py         disparos em lote: destinatários, anexos, imagens, fila de envio
        ├── configuracoes_api.py  rotas /api/configuracoes (e-mail e RADIUS)
        ├── ajustes.py            configurações salvas no banco (com prioridade sobre o .env) e criptografia
        ├── logs_api.py           rotas /api/logs e catálogo de ações
        ├── auditoria.py          registrar() e preenchimento automático do IP
        ├── notificar.py          avisos de novos pedidos de Wi-Fi (Telegram / WhatsApp)
        └── static/               telas: inicio, portal, wifi, wifi-visitante, rede-sem-fio, email,
                                  usuarios, configuracoes, logs, estilo.css, comum.js, contas-ad.js
```

---

## 5. Módulos

| Grupo | Módulo | Endereço | O que faz |
| --- | --- | --- | --- |
| Rede Sem Fio | **Wi-Fi no computador** | `/m/wifi` | Pedidos de liberação do Wi-Fi, liberações por tempo, bloqueios, computadores, pessoas e registro de atividades |
| Rede Sem Fio | **Wi-Fi Visitante** | `/m/wifi-visitante` | Cadastros do portal de visitantes (RADIUS): busca, troca de senha, ativar e desativar |
| E-mails | **E-mails para usuários do AD** | `/m/email?origem=ad` | Disparo em lote para os usuários ativos do AD |
| E-mails | **E-mails por lista (TXT)** | `/m/email?origem=lista` | Disparo em lote para uma lista enviada em arquivo |
| Administração | **Gestão de Usuários** | `/m/usuarios` | Contas do AD e locais: permissões, redefinir senha, desbloquear, reativar |
| Administração | **Configurações** | `/m/configuracoes` | Conta de envio, servidor de e-mail, limites e banco do Wi-Fi Visitante |
| Administração | **Logs** | `/m/logs` | Todas as ações do sistema, com filtros e exportação CSV |

- `/m/rede-sem-fio` mostra os dois módulos do grupo; quem tem só um vai direto para ele.
- O antigo módulo *Reset de senha* foi incorporado à Gestão de Usuários (`/m/senhas` redireciona).
- Todos os módulos têm, no topo, o botão **voltar**, a trilha (Gestão TI › grupo › módulo) e o menu da conta (Início, Sair).

---

## 6. Acesso, permissões e adm geral

### Quem entra

| Tipo de conta | O que acontece no login |
| --- | --- |
| **Equipe da TI** (dentro da OU `AD_OU_PERMITIDA` ou membro do grupo `AD_GRUPO_PERMITIDO`, inclusive grupos aninhados) | No primeiro acesso recebe todos os módulos disponíveis. Não concede permissões, a não ser que o adm geral autorize |
| **Qualquer outra conta ativa do AD** | Entra, mas sem módulos. Vê os módulos de e-mail como *Aguardando liberação* e não consegue fazer nada até receber permissão |
| **Conta desativada no AD** ou bloqueada na Gestão TI | Não entra |
| **Usuário local `admin`** (`ADMIN_USER`) | Acesso de emergência (se `LOGIN_LOCAL_HABILITADO=true`); é adm geral |

### Quem concede permissões

- **Adm geral:** o `admin` local e os logins listados em `ADMIN_GERAL` (separados por vírgula). Tem sempre
  todos os módulos, concede permissões e é o **único** que autoriza outras pessoas a conceder.
- **Autorizados pelo adm geral:** no diálogo **Permissões**, o adm geral marca
  *"Pode conceder permissões a outros usuários"*. A pessoa passa a dar e tirar módulos, bloquear o acesso
  à Gestão TI e criar usuários locais, mas não autoriza ninguém a conceder.
- **Os demais**, mesmo com o módulo Gestão de Usuários, veem a lista e mexem nas contas do AD (senha,
  desbloqueio, reativação), mas não alteram permissões.
- Qualquer usuário pode receber qualquer módulo; o controle está em **quem concede**.
- Ninguém altera as próprias permissões nem o próprio acesso, e só um adm geral altera outro adm geral.
- Tentativas de abrir um módulo sem permissão viram registro `acesso_negado` nos Logs.

---

## 7. Gestão de Usuários

### Lista

- Mostra **todas as contas do AD** e os usuários locais, em abas **Ativos** e **Desativados**, com a contagem de cada uma.
- Etiquetas: **AD** ou **Local**, **TI**, **Adm geral**, **Concede permissões**, **Bloqueada no AD**,
  **Sem uso há N dias · exige SEI**.
- **Busca** por login, nome, CPF (atributo `employeeID`, com ou sem pontuação) ou matrícula (atributo `initials`).
  A busca olha as duas abas: se o resultado estiver na outra, a tela avisa e leva até lá.
- **Filtros rápidos:** com/sem acesso à Gestão TI, equipe da TI, bloqueadas no AD, só AD, só locais.
- O CPF aparece mascarado (`***.456.789-**`) e não sai do servidor completo.

### Ações em contas do AD

| Ação | Detalhes |
| --- | --- |
| **Permissões** | Módulos da pessoa, bloqueio do acesso à Gestão TI e (só adm geral) *Pode conceder permissões* |
| **Redefinir senha** | Senha gerada (ex.: `Folha-Arara-5864!`) ou digitada. Por padrão, exige troca no próximo logon e desbloqueia junto |
| **Desbloquear** | Zera o bloqueio por senha errada (`lockoutTime`), sem trocar a senha |
| **Reativar no AD** | Liga a conta (`userAccountControl`), com senha nova opcional (recomendado: o AD recusa reativar sem senha válida) |

- O motivo ou número do chamado é obrigatório nas ações de conta.
- A senha gerada aparece uma única vez na tela e não é gravada em lugar nenhum.
- Não é possível alterar a própria conta nem contas protegidas do AD (`adminCount=1`, administradores do domínio).

### Regra do SEI (contas sem uso há mais de 90 dias)

- O "sem uso" é contado pelo último logon no AD (`lastLogonTimestamp`) ou, se a conta nunca entrou, pela criação (`whenCreated`).
- Acima de `DIAS_REATIVACAO_SEI` dias (padrão 90), a reativação exige marcar
  **"Estou ciente e a reativação deste usuário possui processo SEI"**, com campo opcional para o número do processo.
- Sem a marcação, o servidor recusa, inclusive se chamado direto pela API.
- A confirmação e o número do SEI ficam nos Logs.
- O `lastLogonTimestamp` replica entre os DCs com até ~14 dias de atraso, o que não afeta uma regra de 90 dias.

### Velocidade

- A lista do AD é lida em segundo plano quando o sistema sobe e fica em memória: a tela abre e busca na hora
  (cerca de 0,1 s com 20 mil contas).
- Quando a lista tem mais de 2 minutos, quem abre recebe a atual e uma atualização roda por trás.
- Depois de redefinir, desbloquear ou reativar, só aquela conta é relida.
- O botão **Atualizar** relê o AD inteiro na hora.
- O log do container mostra o tempo de cada leitura: `Lista do AD carregada: N contas em X s`.

---

## 8. Rede Sem Fio → Wi-Fi no computador

Controla quando um computador da prefeitura pode usar outras redes Wi-Fi além da **PMSL-WIRELESS**.

### Partes

- **Portal do usuário** (`/wifi`): qualquer pessoa entra com o usuário do AD e pede a liberação do computador que está usando.
- **Módulo da TI** (`/m/wifi`): aprova, nega, libera por horas ou dias, personalizado (até `HORAS_MAXIMAS`) ou indeterminado;
  bloqueia de novo; altera o prazo; libera sem pedido; busca pessoas do AD; consulta e exporta o registro de atividades.
- **Script da GPO** (`wlan-filter.ps1`): roda como SYSTEM a cada 5 minutos nas estações, mantém o bloqueio e consulta a API.
- **Avisos** de novos pedidos por Telegram e/ou WhatsApp (Evolution API), se configurados no `.env`.

### Como o portal identifica o computador

Nesta ordem:
1. pelo IP de quem está acessando (informado pelo script da GPO);
2. pelo computador em que o login do AD está logado agora (funciona mesmo atrás do proxy de navegação);
3. pelo nome no atalho (`?h=%ComputerName%`);
4. pelo DNS reverso (PTR), se `DETECTAR_POR_DNS=true`.

Se nada resolver, o portal pede para digitar o nome; esses pedidos aparecem com a etiqueta **nome digitado**.

**Acesso por proxy (ex.: `wifiaprovacao.santaluzia.mg.gov.br`).** Quando o acesso chega por um proxy que não está em
`FORWARDED_ALLOW_IPS`, todo mundo aparece com o IP do proxy. O sistema percebe isso (a requisição traz `X-Forwarded-For`)
e **pula os passos 1 e 4**, que dariam o nome errado (o DNS reverso devolveria o nome do proxy). O log do container avisa:
`Acesso chegando pelo proxy X.X.X.X`. Para voltar a identificar pelo IP, coloque esse IP em `FORWARDED_ALLOW_IPS` e rode
`sudo docker compose up -d`. Isso também corrige o IP gravado nos Logs e o bloqueio de login por IP (sem isso, 5 senhas
erradas de qualquer pessoa bloqueiam o login de todos por 10 minutos).

### Usuário logado em cada computador

- O script informa quem está na sessão física. A aba **Computadores** mostra o usuário, o nome do AD e desde quando.
- A busca aceita login ou nome ("em qual máquina o joaosilva está?"); a aba **Pessoas** mostra *Logado agora em*.
- Pedido feito por alguém diferente de quem está logado recebe a etiqueta **pedido por outra pessoa**.
- Limitações: atualiza a cada 5 minutos, não inclui sessões RDP e, com a tela bloqueada, continua mostrando quem estava logado.

### Sinal fraco e falta de conexão (script da GPO)

- **Ao bloquear, o script não reinicia mais a placa Wi-Fi.** O reinício fazia o Windows voltar no primeiro ponto de acesso
  que encontrasse, com sinal fraco, até trocar sozinho minutos depois. Agora: quem já está na PMSL-WIRELESS não perde a
  conexão; quem está em outra rede é desconectado e conectado direto na PMSL-WIRELESS, e uma varredura nova tira as outras
  redes da lista. A execução em que a troca acontece não conta como "sem conexão".
- **Ao liberar, a placa continua sendo reiniciada**, para as outras redes aparecerem na lista na hora.
- **API sem resposta com o computador liberado:** a liberação é mantida por até `$MinutosToleranciaApi` (30) minutos desde
  a última resposta, respeitando o prazo da liberação. Uma falha rápida de DNS ou a reinicialização do servidor não bloqueia mais.

- O bloqueio só é ativado quando a PMSL-WIRELESS tem sinal ≥ 40%, e só é desfeito se cair abaixo de 30%.
- Se o bloqueio estiver ativo e o computador passar duas execuções seguidas sem conectar na PMSL-WIRELESS,
  libera as outras redes por 30 minutos e tenta de novo.
- Valores no início do `wlan-filter.ps1`: `$SinalMinimo`, `$MargemSinal`, `$ExecucoesSemConexao`, `$MinutosEsperaSemConexao`.
- **Se a API cair**, o script segue a regra normal: bloqueia na prefeitura e libera fora dela.

---

## 9. Rede Sem Fio → Wi-Fi Visitante

O portal do Wi-Fi de visitantes grava os cadastros no banco MySQL `radio` (servidor `10.1.0.4`), usado pelo FreeRADIUS.

### Tabelas usadas

| Tabela | Colunas usadas | Para quê |
| --- | --- | --- |
| `reg_users` | `id`, `idradcheck`, `nome`, `cpf`, `email`, `celular`, `ativo` | Dados do visitante; `idradcheck` liga ao login |
| `radcheck` | `id`, `username`, `attribute`, `value` | Login (normalmente o CPF) e senha do RADIUS |

O formato da senha é definido pela coluna `attribute`. A Gestão TI grava **no mesmo formato que já está lá**:

| `attribute` | Como a senha é gravada |
| --- | --- |
| `MD5-Password` | hash MD5 em hexadecimal (formato atual do portal) |
| `SHA-Password` / `SHA1-Password` | hash SHA-1 |
| `SHA2-Password` / `SHA-256-Password` | hash SHA-256 |
| `Cleartext-Password` | texto puro |

### Busca

- **Nada é carregado ao abrir.** A consulta só acontece com pelo menos 3 caracteres digitados.
- As abas **Ativos** e **Inativos** mostram os totais gerais (guardados por 5 minutos) mesmo antes da busca.
- Como a tabela tem centenas de milhares de cadastros e não tem índices, a busca é direcionada:

| Digitado | Onde procura |
| --- | --- |
| 11 dígitos | CPF ou celular exato, e o login |
| número parcial | início do CPF ou parte do celular |
| texto com `@` | início do e-mail, e o login |
| texto | parte do nome ou início do e-mail |

- Para nos primeiros 500 resultados; se passar, a tela pede para refinar.
- Nomes com acentuação gravada errada (ex.: `JosÃ©`) são corrigidos só na exibição.

**Opcional, para deixar instantâneo** (combine antes com quem mantém o portal, porque altera a tabela dele):

```sql
ALTER TABLE radio.reg_users ADD INDEX idx_cpf (cpf), ADD INDEX idx_email (email), ADD INDEX idx_idradcheck (idradcheck);
```

### Ações

- **Trocar a senha:** gerada (palavra + 4 números, ex.: `praia4821`) ou digitada (mínimo 6 caracteres, sem acento).
  Opções: **reativar** o cadastro junto e **enviar por e-mail** para o endereço do cadastro (sai pela conta de envio).
  O visitante entra com a senha nova na hora. A senha aparece uma vez na tela e nunca é gravada na Gestão TI nem nos logs.
- **Ativar / desativar** o cadastro (`reg_users.ativo`).
- **Histórico** de trocas e ativações, com autor, data e IP.

### Configuração (Configurações → 4. Wi-Fi Visitante)

| Campo | Descrição |
| --- | --- |
| Servidor / porta / banco | `10.1.0.4`, `3306`, `radio` |
| Usuário e senha do MySQL | Conta do banco (recomendado: uma conta própria, não o `root`) |
| Conectar por SSH | Marque se o MySQL só aceita conexões locais. A Gestão TI entra por SSH e conecta em `127.0.0.1` (ou no IP do servidor) por dentro |
| Servidor / porta / usuário / senha SSH | Conta do **Linux** do servidor do banco |
| Tabelas | `reg_users` e `radcheck` (só mude se o portal usar outros nomes) |
| **Testar conexão** | Mostra o total de visitantes e os formatos de senha encontrados |
| **Conferir formato da senha** | Informe login e senha **atual** de um cadastro conhecido; confere se o cálculo bate. Faça uma vez antes de usar |

Conta MySQL recomendada (o comando também aparece na tela):

```sql
CREATE USER 'gestaoti'@'localhost' IDENTIFIED BY 'SENHA-FORTE';
CREATE USER 'gestaoti'@'127.0.0.1' IDENTIFIED BY 'SENHA-FORTE';
GRANT SELECT, UPDATE ON radio.reg_users TO 'gestaoti'@'localhost', 'gestaoti'@'127.0.0.1';
GRANT SELECT, UPDATE ON radio.radcheck  TO 'gestaoti'@'localhost', 'gestaoti'@'127.0.0.1';
FLUSH PRIVILEGES;
```

> O `root` do MySQL em Debian/Ubuntu costuma usar o plugin `auth_socket` e recusa senha (erro 1698).
> Nesse caso, use a conta acima.

---

## 10. E-mails em lote

### Conta de envio

- **Todos os e-mails saem de `geral@santaluzia.mg.gov.br`.** A conta é fixa no código: não muda pela tela nem pelo `.env`.
- O nome do remetente e o "Responder para" podem ser ajustados em cada disparo.
- **Modo de teste** (Configurações → 1. Conta de envio): enquanto a `geral@` não estiver liberada no servidor de e-mail,
  ligue *"Modo de teste: enviar por outra conta"* e informe um e-mail de teste. Com o modo ligado:
  - todos os envios saem desse e-mail (disparos, e-mail de teste e senha de visitante);
  - a tela de disparos mostra a etiqueta **modo de teste**;
  - Configurações mostra um aviso no topo;
  - ligar e desligar fica nos Logs.

### Passo a passo do disparo

1. **Destinatários**
   - **AD:** usuários ativos com e-mail, com busca, filtro por OU e seleção individual ou de todos os filtrados
     (precisa da conta de serviço `AD_BIND_USUARIO`).
   - **TXT:** um e-mail por linha; aceita também `Nome;email`, `email;Nome` e `Nome <email>`.
     Repetidos são removidos e linhas inválidas são mostradas.
2. **Mensagem:** editor com formatação, variáveis `{nome}` e `{email}`, e **Enviar teste para mim**.
   - **Imagens** (botão, Ctrl+V ou arrastar): até `IMAGEM_MAX_MB` cada, embutidas no e-mail. Imagens coladas do Word são convertidas.
   - **Anexos:** até `ANEXOS_MAX_MB` no total. Cada destinatário recebe uma cópia.
3. **Blocos:** quantidade ou tamanho dos blocos; envio **automático** (intervalo em minutos) ou **manual** (*Liberar próximo bloco*).
4. **Revisar e enviar.**

### Acompanhamento

- Aba **Disparos**: progresso, pausar, retomar, cancelar, duplicar e relatório CSV com a situação de cada destinatário.
- O envio roda em segundo plano, com `ENVIO_PAUSA_MS` entre e-mails, e continua de onde parou se o servidor reiniciar.
- Se o servidor de e-mail cair, o disparo é pausado com o motivo na tela.
- Quem não é da TI vê só os próprios disparos; a TI vê todos os dos módulos que tem.
- Confira o limite do provedor (o Microsoft 365, por exemplo, aceita cerca de 10 mil destinatários por dia por caixa) e ajuste blocos e intervalo.

---

## 11. Configurações

A tela é dividida em cartões numerados, com uma barra de **Salvar** fixa no rodapé:

| Cartão | Conteúdo |
| --- | --- |
| **1. Conta de envio** | `geral@` (fixa), modo de teste, forma de login no servidor (a própria conta, outra conta ou sem login) e senha |
| **2. Servidor de e-mail** | Tipo (servidor próprio, Microsoft 365, Google, teste) preenche os campos; servidor, porta, segurança (STARTTLS/SSL/nenhuma). Botões **Testar conexão** e **Enviar e-mail de teste** |
| **3. Limites** | Pausa entre e-mails (em e-mails por minuto), tamanho dos anexos e das imagens, máximo de destinatários |
| **4. Wi-Fi Visitante** | Banco do RADIUS, SSH, tabelas, Testar e Conferir formato (ver seção 9) |

- O que é salvo na tela **tem prioridade sobre o `.env`**. O `.env` vale enquanto nada tiver sido salvo.
- Senhas (SMTP, MySQL, SSH) ficam criptografadas com Fernet, com chave derivada de `SECRET_KEY`, e nunca voltam para a tela.
  **Se trocar a `SECRET_KEY`, digite essas senhas de novo.**
- Toda alteração e todo teste ficam nos Logs.

---

## 12. Logs e auditoria

- Registra tudo, de todos os módulos e pessoas: logins (sucesso e falha), saídas, acessos negados, permissões e autorizações
  para conceder, senhas redefinidas, desbloqueios, reativações (com SEI), pedidos e liberações do Wi-Fi, trocas de senha e
  ativações de visitantes, disparos de e-mail (criação, anexos, imagens, listas lidas, envio, pausa, cancelamento, relatórios),
  configurações alteradas e testadas, e exportações.
- Cada registro: data e hora, módulo, ação, autor (nome e login), IP e detalhe. **Senhas nunca são gravadas.**
- O IP é preenchido automaticamente em cada registro (middleware + evento do SQLAlchemy).
- Filtros por texto, módulo, ação, pessoa e período; cards de resumo (ações hoje, falhas de login, acessos negados…) que abrem a lista filtrada.
- **Exportar CSV** com os filtros aplicados (a exportação também é registrada).

---

## 13. Active Directory

### Conexão e failover

- DCs em `AD_SERVIDORES` (`10.1.100.2,10.1.100.3`).
- `AD_SEGURANCA=auto` tenta LDAPS (636) e depois StartTLS (389). Se os DCs não têm LDAPS, use `starttls` para não perder a primeira tentativa.
- O sistema memoriza qual DC e modo responderam e vai direto neles. O que falhou fica por último por 5 minutos.
- Se o DC cair **no meio** de uma consulta (lista, busca, login), ela é repetida no outro. No log: `DC 10.1.100.x fora do ar: usando o outro servidor do AD`.

### Contas de serviço

| Variável | Para quê | Permissões no AD |
| --- | --- | --- |
| `AD_BIND_USUARIO` / `AD_BIND_SENHA` | Listar usuários (Gestão de Usuários, e-mails do AD, busca de pessoas) | Leitura (usuário comum do domínio) |
| `AD_RESET_USUARIO` / `AD_RESET_SENHA` | Redefinir senha, desbloquear, reativar (opcional; vazio = usa a de cima) | Delegação abaixo |

**Delegação** (Active Directory Users and Computers → botão direito na OU dos usuários → **Delegate Control…**):
1. **Reset user passwords and force password change at next logon**.
2. **Create a custom task → User objects → Write lockoutTime** (desbloquear).
3. **Create a custom task → User objects → Write userAccountControl** (reativar).

**Criptografia obrigatória para senha:** o AD só aceita troca de senha por LDAPS ou StartTLS. Os DCs precisam de certificado
(por exemplo, emitido pela CA do AD — AD CS). `AD_VALIDAR_CERTIFICADO=true` exige certificado confiável.

### Teste pela linha de comando

```bash
sudo docker exec -it gestao-ti-api python -m app.ad seu.login
```

Pede a senha e mostra qual DC e modo funcionaram (`ldaps`, `starttls`, sem criptografia) e se o usuário seria aceito como TI.
Cada execução conta como tentativa de login no AD: não repita com senha errada.

### Atributos usados

| Atributo | Uso |
| --- | --- |
| `sAMAccountName`, `displayName`, `mail`, `department`, `title` | Identificação e e-mails |
| `employeeID` | CPF |
| `initials` | Matrícula |
| `userAccountControl` (bit 2) | Conta desativada / reativar |
| `lockoutTime` | Conta bloqueada / desbloquear |
| `pwdLastSet` | Forçar troca de senha no próximo logon (`0`) |
| `lastLogonTimestamp`, `whenCreated` | Dias sem uso (regra do SEI) |
| `adminCount` | Contas protegidas (não alteradas pela Gestão TI) |
| `memberOf` (regra `1.2.840.113556.1.4.1941`) | Grupo da TI, incluindo grupos aninhados |

---

## 14. Variáveis do `.env`

Depois de editar: `cd /opt/gestao-ti && sudo docker compose up -d`.

### Essenciais

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `DB_PASSWORD` | aleatória | Senha do PostgreSQL da Gestão TI |
| `AGENT_KEY` | aleatória | Chave do script das estações (na migração, é copiada do sistema antigo) |
| `SECRET_KEY` | aleatória | Assina as sessões e deriva a chave de criptografia dos segredos |
| `ADMIN_USER` / `ADMIN_NOME` / `ADMIN_PASSWORD` | `admin` | Usuário local de emergência (a senha só vale na criação) |
| `ADMIN_GERAL` | vazio | Logins do AD que são adm geral, separados por vírgula |
| `PORTA` | `8090` | Porta publicada no servidor |
| `PAINEL_URL` | `http://IP:8090` | Endereço da Gestão TI (links dos avisos do Telegram/WhatsApp) |
| `AGENTE_URL` | `http://wifiaprovacao.santaluzia.int:8090` | Endereço que o script das estações usa para consultar a API. Direto no servidor, **sem** proxy. Vazio = usa o `PAINEL_URL` |
| `COOKIE_SEGURO` | `false` | `true` quando estiver atrás de HTTPS |
| `SESSAO_HORAS` | `12` | Duração da sessão |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs do proxy reverso confiável (para obter o IP real do usuário) |
| `HOSTS_PORTAL_WIFI` | `wifiaprovacao,wifi-aprovacao` | Endereços antigos que abrem direto o portal do Wi-Fi |
| `LOGIN_LOCAL_HABILITADO` | `true` | Permite o login do `admin` local |

### Active Directory

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `AD_HABILITADO` | `true` | Liga o login pelo AD |
| `AD_SERVIDORES` | `10.1.100.2,10.1.100.3` | DCs, em ordem |
| `AD_DOMINIO` / `AD_BASE_DN` | `santaluzia.int` / `DC=santaluzia,DC=int` | Domínio e base de busca |
| `AD_OU_PERMITIDA` | OU da Infra | Quem é da equipe da TI (inclui sub-OUs) |
| `AD_GRUPO_PERMITIDO` | vazio | DN de grupo da TI (alternativa ou complemento) |
| `AD_SEGURANCA` | `auto` | `auto`, `ldaps`, `starttls` ou `nenhuma` |
| `AD_PERMITIR_SEM_TLS` | `false` | Permite conexão sem criptografia (não serve para trocar senha) |
| `AD_VALIDAR_CERTIFICADO` | `false` | Exige certificado confiável dos DCs |
| `AD_TIMEOUT` | `5` | Tempo limite por DC (segundos) |
| `AD_BIND_USUARIO` / `AD_BIND_SENHA` | vazio | Conta de serviço de leitura |
| `AD_RESET_USUARIO` / `AD_RESET_SENHA` | vazio | Conta para senhas e reativação |
| `DIAS_REATIVACAO_SEI` | `90` | Dias sem uso a partir dos quais a reativação exige SEI |

### Wi-Fi no computador

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `HORAS_PERMITIDAS` / `DIAS_PERMITIDOS` | `1,2,4,8` / `1,2,3,5,7` | Opções do portal |
| `PERSONALIZADO_USUARIO` / `HORAS_MAXIMAS_USUARIO` | `true` / `720` | Tempo personalizado no portal e máximo |
| `HORAS_ADMIN` / `DIAS_ADMIN` / `HORAS_MAXIMAS` | `1,2,4,8,12` / `1,2,3,7,15,30` / `720` | Opções e máximo da TI |
| `PERMITIR_INDETERMINADO` | `true` | Liberação até a TI bloquear |
| `MAX_SOLICITACOES_POR_DIA` | `5` | Pedidos por computador ou pessoa em 24 h |
| `DETECTAR_POR_DNS` / `DNS_SERVIDORES` | `true` / DCs | Identificação por DNS reverso |
| `MINUTOS_DETECCAO_COMPUTADOR` | `30` | Validade do último IP informado pelo script |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | vazio | Avisos de pedidos no Telegram |
| `EVOLUTION_URL` / `EVOLUTION_INSTANCE` / `EVOLUTION_APIKEY` / `WHATSAPP_NUMEROS` | vazio | Avisos no WhatsApp |

### E-mail (podem ser sobrescritas pela tela)

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `SMTP_HOST` / `SMTP_PORT` | `mailpit` / `1025` | Servidor de e-mail |
| `SMTP_TLS` / `SMTP_SSL` | `false` / `false` | STARTTLS (587) ou SSL (465) |
| `SMTP_USER` / `SMTP_PASSWORD` | vazio | Login no servidor |
| `SMTP_FROM` | `geral@...` | Informativo: a conta é fixa no código |
| `SMTP_FROM_NOME` | Prefeitura Municipal de Santa Luzia | Nome do remetente |
| `ENVIO_PAUSA_MS` | `200` | Pausa entre e-mails (200 ms ≈ 300/min) |
| `ANEXOS_MAX_MB` / `IMAGEM_MAX_MB` / `LISTA_MAX_EMAILS` | `10` / `3` / `50000` | Limites dos disparos |
| `CACHE_AD_MINUTOS` | `10` | Validade da lista de e-mails do AD em memória |

### Wi-Fi Visitante (podem ser sobrescritas pela tela, que é o recomendado)

`RADIUS_DB_HOST` (`10.1.0.4`), `RADIUS_DB_PORTA` (`3306`), `RADIUS_DB_NOME` (`radio`), `RADIUS_DB_USUARIO`,
`RADIUS_DB_SENHA`, `RADIUS_SSH` (`nao`/`sim`), `RADIUS_SSH_HOST`, `RADIUS_SSH_PORTA` (`22`), `RADIUS_SSH_USUARIO`,
`RADIUS_SSH_SENHA`, `RADIUS_TABELA_USUARIOS` (`reg_users`), `RADIUS_TABELA_SENHAS` (`radcheck`).

### Textos e marca

`APP_NOME`, `APP_SUBTITULO`, `WIFI_NOME`, `WIFI_SUBTITULO`, `ORGAO_LINHA1`, `ORGAO_LINHA2`, `RODAPE`.
Para o logo, coloque `logo.svg` ou `logo.png` em `/opt/gestao-ti/marca/` (não precisa reiniciar).

---

## 15. Instalação local (testes)

Requisitos: Docker com o plugin `docker compose`.

```bash
unzip gestao-ti.zip && cd gestao-ti
chmod +x aplicar_gestao_ti.sh
sudo MODO=local ./aplicar_gestao_ti.sh
```

- Gestão TI: `http://localhost:8090`. Usuário `admin` e senha aparecem no fim e ficam em `/opt/gestao-ti/CREDENCIAIS-INICIAIS.txt`
  (se o `.env` veio no pacote, vale a `ADMIN_PASSWORD` dele).
- Caixa de e-mail falsa (Mailpit): `http://localhost:8025`. Nada sai de verdade.
- O login pelo AD funciona se a máquina enxergar os DCs.
- Para o Wi-Fi Visitante, a máquina precisa alcançar `10.1.0.4` (porta 3306 ou 22, se usar SSH).

---

## 16. Subida no servidor (troca do sistema antigo)

Servidor: **10.1.100.129**. Sistema antigo em `/opt/pmsl-wifi-aprovacao` (porta 8090). A Gestão TI fica em `/opt/gestao-ti`.

```bash
# na sua máquina
scp gestao-ti.zip suporte@10.1.100.129:~
ssh suporte@10.1.100.129

# no servidor
sudo unzip -q ~/gestao-ti.zip -d /opt
cd /opt/gestao-ti
sudo chmod 600 .env
sudo chmod +x migrar_de_wifiaprovacao.sh aplicar_gestao_ti.sh
sudo URL_GESTAO=http://10.1.100.129:8090 ./migrar_de_wifiaprovacao.sh
```

### O que o script faz

1. **Backup** do banco antigo em `/opt/backups/wifi_aprovacao-DATA.sql.gz`.
2. Prepara o `.env` com a **mesma `AGENT_KEY`** do sistema antigo (as estações continuam funcionando sem mudar nada),
   copia `COOKIE_SEGURO`, `FORWARDED_ALLOW_IPS` e o logo, e gera `DB_PASSWORD` se estiver vazio.
3. Sobe a Gestão TI na porta provisória **18090**, com o sistema antigo ainda atendendo.
4. Para a API antiga e copia os dados: pedidos e liberações (mesmos números), histórico, computadores e administradores.
   Copia só as colunas que existem nos dois bancos.
5. A Gestão TI assume a porta **8090** (as estações ficam menos de 1 minuto sem resposta e mantêm o estado atual).
6. Confere `/health` e o endpoint das estações e desliga o sistema antigo. O banco antigo fica guardado.

### Opções do script

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `URL_GESTAO` | `https://gestaoti.santaluzia.mg.gov.br` | Endereço gravado em `PAINEL_URL` |
| `ANTIGO_DIR` | `/opt/pmsl-wifi-aprovacao` | Pasta do sistema antigo |
| `DESTINO` | `/opt/gestao-ti` | Pasta da Gestão TI |
| `SO_DADOS=1` | — | Só copia os dados (Gestão TI já no ar) |
| `FORCAR=1` | — | Copia de novo mesmo se já tiver sido feita (sem duplicar) |

### Se der errado

- Se algo falhar depois do passo 4, o script religa o sistema antigo sozinho.
- Manualmente: `cd /opt/gestao-ti && sudo docker compose stop` e `cd /opt/pmsl-wifi-aprovacao && sudo docker compose up -d`.

### Conferência

```bash
curl -s http://127.0.0.1:8090/health
sudo docker ps --format '{{.Names}} {{.Status}} {{.Ports}}'
sudo docker logs --since 10m gestao-ti-api | grep -c agente/liberacao   # maior que 0 = estações conversando
```

### Depois da troca, no navegador

1. Entre com o `admin` local e coloque seu login em `ADMIN_GERAL` no `.env` (`sudo docker compose up -d` em seguida).
2. Configurações → conta de envio e servidor de e-mail (ou modo de teste).
3. Configurações → Wi-Fi Visitante → banco do RADIUS → Testar → Conferir formato.
4. Gestão de Usuários → confira as permissões da equipe.
5. Teste o reset de senha: `sudo docker exec -it gestao-ti-api python -m app.ad seu.login`.

---

## 17. Atualização, backup e restauração

### Atualizar para uma versão nova

```bash
sudo unzip -oq ~/gestao-ti.zip -d /tmp/gestao-ti-nova
cd /tmp/gestao-ti-nova/gestao-ti
sudo ./aplicar_gestao_ti.sh
```

O banco e o `.env` são mantidos; configurações novas são **acrescentadas** ao `.env`, sem alterar as existentes.
O script regenera `saida-windows/`.

### Backup

```bash
sudo mkdir -p /opt/backups
sudo docker exec gestao-ti-db pg_dump -U gestao gestao_ti | gzip > /opt/backups/gestao_ti-$(date +%F).sql.gz
sudo cp /opt/gestao-ti/.env /opt/backups/gestao-ti.env-$(date +%F)
```

Agendar todo dia às 2h (`sudo crontab -e`):

```
0 2 * * * docker exec gestao-ti-db pg_dump -U gestao gestao_ti | gzip > /opt/backups/gestao_ti-$(date +\%F).sql.gz
```

### Restaurar

```bash
cd /opt/gestao-ti
sudo docker compose stop api
gunzip -c /opt/backups/gestao_ti-AAAA-MM-DD.sql.gz | sudo docker exec -i gestao-ti-db psql -U gestao -d gestao_ti
sudo docker compose start api
```

(Para restaurar num banco limpo, recrie antes: `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`.)

---

## 18. Endereço gestaoti e proxy reverso

1. Pedir à equipe de redes o DNS `gestaoti.santaluzia.mg.gov.br` e o proxy reverso para `10.1.100.129:8090`.
2. Quando abrir pelo endereço novo:
   - `PAINEL_URL=https://gestaoti.santaluzia.mg.gov.br` no `.env`;
   - `COOKIE_SEGURO=true` (se for HTTPS);
   - `FORWARDED_ALLOW_IPS=IP_DO_PROXY`;
   - `sudo ./aplicar_gestao_ti.sh` (regenera o script da GPO com o endereço novo).
3. Só então atualizar o script no NETLOGON e o atalho da GPO (seção 19).
4. O endereço antigo (`wifiaprovacao...`) pode continuar apontando para o mesmo servidor: quem abrir por ele cai no portal do Wi-Fi,
   e o atalho antigo (`/?h=COMPUTADOR`) continua funcionando.

O proxy precisa repassar os cabeçalhos `X-Forwarded-For` e `X-Forwarded-Proto` e aceitar uploads de pelo menos 15 MB (anexos).

---

## 19. Estações Windows (GPO)

1. Copie `/opt/gestao-ti/saida-windows/wlan-filter.ps1` para `\\santaluzia.int\NETLOGON\PMSL-WiFi`, renomeando o atual:
   ```bash
   sudo apt install -y smbclient
   cd /opt/gestao-ti/saida-windows
   smbclient //10.1.100.2/NETLOGON -U 'SANTA-LUZIA\seu.usuario' \
     -c 'cd PMSL-WiFi; rename wlan-filter.ps1 wlan-filter.ps1.anterior; put wlan-filter.ps1; ls'
   ```
2. No GPO **WiFi - Prioridade PMSL-WIRELESS**, atalho do portal em
   **Computer Configuration → Preferences → Windows Settings → Shortcuts → New → Shortcut**:
   - **Action:** Replace
   - **Name:** `Solicitar Acesso à Rede Sem Fio`
   - **Target type:** URL
   - **Location:** All Users Start Menu (e/ou All Users Desktop)
   - **Target URL:** `https://gestaoti.santaluzia.mg.gov.br/wifi?h=%ComputerName%`
   - Aba **Common:** marque **Remove this item when it is no longer applied**

Enquanto a URL nova não estiver no ar, **não** troque o script: o atual (mesma `AGENT_KEY`, porta 8090) continua funcionando.

---

## 20. Endereços das telas e da API

### Telas

| Endereço | Tela |
| --- | --- |
| `/` | Login e início com os módulos (hosts antigos e `?h=` vão para `/wifi`) |
| `/wifi` | Portal público do Wi-Fi |
| `/m/rede-sem-fio`, `/m/wifi`, `/m/wifi-visitante` | Rede Sem Fio |
| `/m/email?origem=ad`, `/m/email?origem=lista` | E-mails em lote |
| `/m/usuarios`, `/m/configuracoes`, `/m/logs` | Administração |
| `/m/senhas`, `/admin` | Redirecionamentos de compatibilidade |
| `/health` | Situação da aplicação |

### API (todas exigem sessão, exceto portal, agente e health)

| Prefixo | Rotas principais |
| --- | --- |
| `/api/auth` | `POST login`, `POST logout`, `GET eu` (usuário, módulos, `admin_geral`, `pode_conceder`, `aguardando`) |
| `/api/portal` | `login`, `logout`, `estado`, `solicitacoes`, `solicitacoes/{id}/cancelar` |
| `/api/agente/liberacao` | Consulta das estações (cabeçalho `X-Agent-Key`) — **não mudou em relação ao sistema antigo** |
| `/api/admin` | `solicitacoes` (+ `aprovar`, `negar`, `bloquear`, `alterar-prazo`), `liberacoes`, `computadores`, `pessoas`, `eventos`, `eventos.csv` |
| `/api/usuarios` | `GET /` (locais), `diretorio` (AD em cache: `q`, `situacao`, `filtro`, `limite`, `atualizar`), `PUT ad/{login}/modulos`, `buscar-ad`, `POST /`, `POST local`, `PATCH {id}`, `eventos` |
| `/api/senhas` | `buscar`, `conta/{login}`, `redefinir`, `desbloquear`, `reativar` (`ciente_sei`, `processo_sei`), `historico` |
| `/api/visitante` | `visitantes` (`q` ≥ 3 caracteres), `totais`, `visitantes/{id}/trocar-senha`, `visitantes/{id}/ativo`, `historico` |
| `/api/email` | `config`, `ad/usuarios`, `lista/ler`, `imagens`, `remetentes`, `teste`, `disparos` (+ `anexos`, `iniciar`, `proximo-bloco`, `pausar`, `retomar`, `cancelar`, `destinatarios`, `relatorio.csv`) |
| `/api/configuracoes` | `email` (GET/PUT), `email/testar`, `email/enviar-teste`, `radius` (GET/PUT), `radius/testar`, `radius/conferir` |
| `/api/logs` | `GET /` (filtros e paginação), `opcoes`, `resumo`, `exportar.csv` |

A documentação interativa do FastAPI fica desativada em produção; as rotas acima são a referência.

---

## 21. Banco de dados

PostgreSQL `gestao_ti`, usuário `gestao`. As tabelas são criadas e as colunas novas acrescentadas automaticamente na subida.

| Tabela | Conteúdo |
| --- | --- |
| `administradores` | Usuários da Gestão TI (locais e do AD): origem, ativo, `pode_conceder`, hash da senha (só locais) |
| `permissoes` | Módulos de cada usuário |
| `solicitacoes` | Pedidos e liberações do Wi-Fi no computador |
| `computadores` | Computadores informados pelo script da GPO (IP, usuário logado, última vez) |
| `eventos` | Registro de todas as ações (base do módulo Logs) |
| `disparos`, `disparo_destinatarios`, `disparo_anexos` | E-mails em lote |
| `configuracoes` | Ajustes salvos pela tela (segredos criptografados) |
| `redes_visitante` | Legado (não usada) |

---

## 22. Segurança

- **Senhas:** nunca gravadas nos logs. Locais usam bcrypt; segredos de integração usam Fernet (chave derivada de `SECRET_KEY`).
- **Sessão:** cookie assinado, `HttpOnly`, `SameSite`; `Secure` quando `COOKIE_SEGURO=true`.
- **Login:** bloqueio de 10 minutos após 5 senhas erradas no mesmo IP; todas as tentativas registradas.
- **Ninguém se libera sozinho no Wi-Fi:** o portal só cria o pedido; a liberação exige decisão da TI.
- **`AGENT_KEY`:** fica no script do NETLOGON (legível pelo domínio). Com ela só se consulta se um computador está liberado e
  se informa nome e IP; não libera nada.
- **IP real:** vem da conexão; cabeçalhos de proxy só são aceitos de `FORWARDED_ALLOW_IPS`.
- **AD:** troca de senha só por conexão criptografada; contas protegidas e a própria conta não podem ser alteradas.
- **RADIUS:** use uma conta MySQL com `SELECT, UPDATE` só nas duas tabelas.
- **`.env`:** permissão `600`, fora de qualquer repositório. Se alguma chave foi compartilhada (chat, e-mail), gere outra:
  `AGENT_KEY` (exige atualizar o script do NETLOGON), `SECRET_KEY` (derruba as sessões e exige redigitar as senhas da tela de
  Configurações) e a senha do `admin`.
- **Container:** a API roda como usuário sem privilégios (`appuser`, UID 10001).

---

## 23. Solução de problemas

| Sintoma | Causa provável | O que fazer |
| --- | --- | --- |
| Login do AD falha para todos | DCs inacessíveis ou certificado | `docker exec -it gestao-ti-api python -m app.ad seu.login`; ajuste `AD_SEGURANCA` |
| "Redefinir senha" diz que precisa de conexão criptografada | DCs sem LDAPS/StartTLS | Instalar certificado nos DCs (AD CS) |
| "Acesso negado" ao redefinir/reativar | Falta delegação | Ver seção 13 (Delegate Control) |
| Lista do AD vazia | `AD_BIND_USUARIO` vazio ou senha errada | Preencher no `.env` e `docker compose up -d` |
| Wi-Fi Visitante: erro 1698 | `root` do MySQL usa `auth_socket` | Criar a conta `gestaoti` (seção 9) |
| Wi-Fi Visitante: erro 1045/1044 | Usuário ou senha do MySQL, ou sem permissão no banco | Conferir conta e `GRANT` |
| Wi-Fi Visitante: 2003/2013 ou timeout | Porta 3306 fechada ou busca muito ampla | Ativar SSH; refinar a busca; considerar os índices opcionais |
| E-mail de teste não chega | SMTP ou `geral@` não liberada | Testar conexão; usar o modo de teste |
| Disparo pausado sozinho | Servidor de e-mail caiu ou recusou | Ver o motivo na aba Disparos e retomar |
| Estações não consultam a API | `AGENT_KEY` diferente ou porta/URL errada | `docker logs gestao-ti-api \| grep agente/liberacao`; comparar a chave do `.env` com a do script |
| Senhas da tela "sumiram" | `SECRET_KEY` foi trocada | Digitar de novo em Configurações |
| IP de todos aparece como o do proxy | `FORWARDED_ALLOW_IPS` sem o IP do proxy | Ajustar e reiniciar |

---

## 24. Comandos úteis

```bash
cd /opt/gestao-ti
sudo docker compose ps                          # situação
sudo docker compose logs -f api                 # logs em tempo real
sudo docker compose up -d                       # aplicar mudanças do .env
sudo docker compose restart api                 # reiniciar a aplicação
sudo docker compose --profile teste up -d       # subir com o Mailpit (testes)
sudo docker exec -it gestao-ti-api python -m app.ad seu.login     # testar o AD
sudo docker exec -it gestao-ti-db psql -U gestao gestao_ti         # console do banco
```

**Senha do `admin` local esquecida:**

```bash
sudo docker exec -it gestao-ti-api python -c "from app.auth import gerar_hash; from app.db import *; s=SessionLocal(); a=s.query(Administrador).filter_by(usuario='admin').one(); a.senha_hash=gerar_hash('NovaSenha123'); a.ativo=True; s.commit()"
```

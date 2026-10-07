# Gestão TI (PMSL)

Plataforma da TI da Prefeitura de Santa Luzia, dividida em **módulos**. Todos entram com o usuário do AD, e cada pessoa vê só os módulos que a TI liberou para ela.

| Módulo | Quem pode receber | O que faz |
| --- | --- | --- |
| **Rede Sem Fio → Wi-Fi no computador** | só TI | Pedidos de liberação do Wi-Fi, computadores, bloqueios e registro de atividades |
| **Rede Sem Fio → Wi-Fi Visitante** | só TI | Visitantes cadastrados no portal do Wi-Fi: trocar a senha, ativar e desativar |
| **E-mails para usuários do AD** | só TI | Disparo em lote para os e-mails dos usuários ativos do AD |
| **E-mails por lista (TXT)** | qualquer pessoa liberada | Disparo em lote para uma lista enviada em arquivo TXT |
| **Configurações** | só TI | Servidor de e-mail (SMTP), limites dos disparos e banco do Wi-Fi Visitante |
| **Gestão de Usuários** | só TI | Todos os usuários do AD e os locais: permissões, redefinir senha, desbloquear e reativar contas do AD |
| **Logs** | só TI | Todas as ações feitas na Gestão TI, por qualquer usuário, em todos os módulos |

**Equipe da TI** é quem está na OU `AD_OU_PERMITIDA` (ou no grupo `AD_GRUPO_PERMITIDO`). No primeiro acesso, essas pessoas recebem os módulos padrão, mas **não** podem conceder permissões.

**Qualquer outra conta ativa do AD também consegue entrar**, mas sem permissão nenhuma. Ela vê os módulos de e-mail marcados como "Aguardando liberação" e não consegue usar nada até receber permissão.

### Quem concede permissões

- **Adm geral:** o usuário local `ADMIN_USER` (`admin`) e os logins do AD listados em `ADMIN_GERAL` no `.env`, separados por vírgula (ex.: `ADMIN_GERAL=bernardo.medeiros`). Ele tem sempre todos os módulos, concede permissões e é o **único** que autoriza outras pessoas a conceder.
- **Autorizados pelo adm geral:** no diálogo **Permissões**, o adm geral marca **"Pode conceder permissões a outros usuários"**. A pessoa passa a dar e tirar módulos, bloquear o acesso à Gestão TI e criar usuários locais, mas não autoriza ninguém.
- **Os demais**, mesmo com o módulo Gestão de Usuários, veem a lista e mexem nas contas do AD (senha, desbloqueio, reativação), mas não alteram permissões.
- **Qualquer usuário pode receber qualquer módulo.** A antiga trava "fora da TI só recebe E-mails por lista" não existe mais: o controle passou a ser de quem concede.
- Ninguém altera as próprias permissões nem o próprio acesso, e só um adm geral altera outro adm geral.
- Cada concessão e cada autorização ficam nos **Logs** e na aba **Acessos e alterações**.

## Gestão de Usuários

Substitui o antigo módulo **Reset de senha**: tudo o que ele fazia está aqui, e o endereço `/m/senhas` agora leva para cá.


A tela mostra **todas as contas do AD** e os usuários locais, separados em abas **Ativos** e **Desativados**, com a contagem de cada uma. Cada pessoa tem a etiqueta **AD** ou **Local**, e a etiqueta **TI** quando é da equipe.
- **Busca** por login, nome, CPF ou matrícula. O CPF vem do campo `employeeID` do AD e é aceito com ou sem pontos; a matrícula vem do campo `initials`. A busca olha as duas abas: se o resultado estiver na outra, a tela avisa e leva até lá com um clique.
- **Filtro rápido:** com ou sem acesso à Gestão TI, equipe da TI, contas bloqueadas no AD, só AD ou só locais.
- **Ações nas contas do AD:** Permissões, Redefinir senha, Desbloquear e **Reativar no AD**. O Reativar pode definir uma senha nova junto, e é o recomendado: o AD recusa reativar uma conta sem senha válida. Contas protegidas (administradores do domínio) não podem ser alteradas por aqui.
- **Contas sem uso há mais de 90 dias** (configurável em `DIAS_REATIVACAO_SEI`) só são reativadas com processo SEI. A conta aparece com a etiqueta "Sem uso há N dias · exige SEI". Ao reativar, a TI precisa marcar **"Estou ciente e a reativação deste usuário possui processo SEI"**, e pode informar o número do processo. Sem a marcação, o sistema recusa, inclusive pela API. A confirmação e o número do SEI ficam nos Logs. O "sem uso" é contado a partir do último logon no AD (`lastLogonTimestamp`), ou da criação da conta se ela nunca entrou. Esse campo replica entre os DCs com até ~14 dias de atraso, o que não atrapalha uma regra de 90 dias.
- **Bloquear acesso à Gestão TI** vale só para o sistema; a conta no AD não muda.
- O CPF aparece mascarado na tela (`***.456.789-**`) e não sai do servidor completo.
- **Velocidade:** a lista do AD é lida em segundo plano assim que o sistema sobe e fica em memória, então a tela abre e busca na hora (cerca de 0,1 s com 20 mil contas no teste). Quando a lista passa de 2 minutos, quem abre a tela recebe a atual e uma atualização roda por trás. Depois de redefinir, desbloquear ou reativar, só aquela conta é atualizada. O botão **Atualizar** relê o AD inteiro na hora.
- O sistema memoriza qual DC e qual modo (LDAPS/StartTLS) responderam e vai direto neles. Um DC ou modo que falhou fica por último por 5 minutos. Se os DCs não têm LDAPS, defina `AD_SEGURANCA=starttls` no `.env` para não perder nem a primeira tentativa. O tempo de cada leitura completa aparece no log do container (`Lista do AD carregada: N contas em X s`).

## Endereços

- `/` — login e tela inicial com os módulos.
- `/m/rede-sem-fio`: tela com os dois módulos da Rede Sem Fio. Quem tem só um deles vai direto para ele.
- `/m/wifi`, `/m/wifi-visitante`, `/m/email?origem=ad`, `/m/email?origem=lista`, `/m/usuarios` — páginas dos módulos.
- `/wifi` — portal público do Wi-Fi, onde qualquer usuário do AD pede a liberação do computador. O atalho do GPO aponta para cá, com `?h=%ComputerName%`.
- `/admin` — redireciona para `/m/wifi`, por compatibilidade.
- `/api/agente/liberacao` — consulta do script das estações. **Não mudou.**

## Subir no servidor no lugar do sistema antigo do Wi-Fi

O servidor 10.1.100.129 já roda o sistema antigo (`/opt/pmsl-wifi-aprovacao`, porta 8090). O script `migrar_de_wifiaprovacao.sh` troca um pelo outro e copia os dados:

```bash
# no servidor, com o zip copiado para lá
unzip gestao-ti.zip && cd gestao-ti
chmod +x migrar_de_wifiaprovacao.sh aplicar_gestao_ti.sh
sudo ./migrar_de_wifiaprovacao.sh
```

O script faz, nesta ordem:
1. **Backup** do banco antigo em `/opt/backups/wifi_aprovacao-DATA.sql.gz`.
2. Prepara o `.env` da Gestão TI em `/opt/gestao-ti`, com a **mesma `AGENT_KEY`** do sistema antigo, para que os scripts das estações continuem funcionando sem mexer em nada. Também copia do sistema antigo `COOKIE_SEGURO` e `FORWARDED_ALLOW_IPS`, e o logo.
3. Instala a Gestão TI na porta provisória **18090**, enquanto o sistema antigo continua atendendo.
4. Desliga a API antiga e copia os dados: pedidos e liberações com os mesmos números, histórico, computadores e administradores. Copia só as colunas que existem nos dois bancos e não duplica nada se rodar de novo.
5. A Gestão TI assume a porta **8090**. As estações ficam menos de 1 minuto sem resposta e, nesse intervalo, mantêm o estado atual.
6. Confere `/health` e o endpoint das estações e desliga o sistema antigo. O banco antigo fica guardado.

Se algo falhar depois do passo 4, o script religa o sistema antigo sozinho. Para voltar manualmente:
`cd /opt/gestao-ti && sudo docker compose stop` e depois `cd /opt/pmsl-wifi-aprovacao && sudo docker compose up -d`.

**Endereços:**
- `gestaoti.santaluzia.mg.gov.br`: pedir à equipe de redes DNS e proxy reverso apontando para `10.1.100.129:8090`.
- O endereço antigo (`wifiaprovacao...`) pode continuar apontando para o mesmo lugar: quem abrir por ele cai direto no **portal do Wi-Fi**. O atalho da GPO (`/?h=COMPUTADOR`) também continua funcionando.

**Depois da troca, no navegador:**
1. Configurações → servidor de e-mail, senha da conta e, se for o caso, modo de teste.
2. Configurações → Wi-Fi Visitante → banco do portal. O banco da Gestão TI no servidor é novo, então essas configurações não vêm da máquina de testes.
3. No `.env` (`/opt/gestao-ti/.env`), coloque seu login em `ADMIN_GERAL` e rode `cd /opt/gestao-ti && sudo docker compose up -d`.

## Instalação local (para testes)

Precisa de Docker com o plugin `docker compose`. Na pasta extraída do zip:

```bash
chmod +x aplicar_gestao_ti.sh
sudo MODO=local ./aplicar_gestao_ti.sh
```

- Gestão TI: `http://localhost:8090`. O usuário `admin` e a senha aparecem no final da instalação e ficam em `/opt/gestao-ti/CREDENCIAIS-INICIAIS.txt`.
- **Caixa de e-mail de teste (Mailpit): `http://localhost:8025`.** Todo e-mail disparado cai aqui, e **nada sai de verdade**.
- O login pelo AD funciona se a máquina enxergar os DCs (10.1.100.2 e 10.1.100.3).
- Para o módulo **E-mails para usuários do AD**, preencha a conta de serviço `AD_BIND_USUARIO` e `AD_BIND_SENHA` no `/opt/gestao-ti/.env` e rode `cd /opt/gestao-ti && sudo docker compose --profile teste up -d`.

## Wi-Fi Visitante

O portal do Wi-Fi de visitantes grava os cadastros num banco MySQL (`radio`, servidor 10.1.0.4):
- **`reg_users`**: nome, CPF, e-mail, celular, `ativo` e `idradcheck`, que liga o cadastro ao login;
- **`radcheck`**: o login (`username`, normalmente o CPF) e a senha (`value`) do RADIUS. A senha fica em código, conforme a coluna `attribute`, por exemplo `MD5-Password` = hash MD5.

O módulo **não carrega ninguém ao abrir**: a consulta ao banco só acontece depois de digitar pelo menos 3 caracteres do nome, CPF, e-mail ou celular. O resultado aparece separado nas abas **Ativos** e **Inativos**.

**Como a busca procura.** A tabela do portal tem centenas de milhares de cadastros e não tem índices nessas colunas, então a busca é direcionada pelo que foi digitado:
- **11 dígitos:** CPF ou celular exato, e também o login;
- **número parcial:** início do CPF ou parte do celular;
- **texto com @:** início do e-mail, e também o login;
- **texto:** parte do nome ou início do e-mail.

Ela para nos primeiros 500 resultados; se passar disso, a tela pede para digitar mais.

**Opcional, para acelerar:** índices na tabela do portal deixam a busca instantânea. Combine antes com quem mantém o portal, porque altera a tabela dele:

    ALTER TABLE radio.reg_users ADD INDEX idx_cpf (cpf), ADD INDEX idx_email (email), ADD INDEX idx_idradcheck (idradcheck);

O módulo permite:
- **Trocar a senha:** gerada (fácil de digitar, ex.: `praia4821`) ou digitada (mínimo 6 caracteres). A Gestão TI grava a senha em `radcheck` **no mesmo formato que já está lá**, e o visitante entra com a senha nova na hora. Opções: reativar o cadastro junto, se estiver inativo, e mandar a senha nova para o e-mail do cadastro (sai pela `geral@`). A senha aparece uma única vez na tela e nunca é gravada na Gestão TI nem nos logs.
- **Ativar ou desativar** o cadastro (`reg_users.ativo`).
- **Histórico** de trocas e ativações, com quem fez, quando e de qual IP.

**Configuração** em Configurações → **4. Wi-Fi Visitante**, com a explicação de cada campo na tela:
- **Banco:** servidor, porta, nome do banco, usuário e senha do **MySQL**.
- **Conectar por SSH:** marque se o MySQL só aceita conexões do próprio servidor, o padrão do `root`. Informe o usuário e a senha do **Linux** do servidor; a Gestão TI entra por SSH e fala com o banco por dentro.
- **Testar conexão:** mostra quantos visitantes existem e em qual formato as senhas estão gravadas.
- **Conferir formato da senha:** informe o login e a senha **atual** de um cadastro seu. O sistema confere se o cálculo bate com o gravado. **Faça isso uma vez antes de usar o módulo.**
- **Recomendado:** em vez do `root`, crie um usuário do MySQL só com `SELECT, UPDATE` nas duas tabelas. O comando está na própria tela.

As senhas do banco e do SSH ficam criptografadas no banco da Gestão TI.

## Logs

Reúne tudo o que fica registrado na Gestão TI, de todos os módulos e de todas as pessoas: entradas e falhas de login, saídas, tentativas de abrir um módulo sem permissão, permissões alteradas, senhas redefinidas, contas desbloqueadas e reativadas (com o SEI), pedidos e liberações do Wi-Fi, troca de senha e ativação de visitantes do Wi-Fi, disparos de e-mail (criação, anexos, envio, pausa, cancelamento, relatórios), configurações alteradas e testadas e exportações.
- Cada registro mostra quando, o módulo, a ação, quem fez (nome e login), o IP e o detalhe. **Senhas nunca são gravadas.**
- Filtros por texto, módulo, ação, pessoa e período. Os cards do topo (ações hoje, falhas de login, acessos negados…) abrem a lista já filtrada.
- **Exportar CSV** com os filtros aplicados. A própria exportação também fica registrada.
- Na atualização, quem já tem o módulo **Gestão de Usuários** recebe o **Logs**.

### Senhas e contas do AD

- **Redefinir senha.** O sistema **gera** uma senha temporária fácil de ditar (ex.: `Folha-Arara-5864!`), ou a TI **digita** uma. Por padrão, a pessoa tem de trocar a senha no próximo logon, e a conta é desbloqueada junto, se estiver bloqueada. A senha gerada aparece uma única vez na tela e **não é gravada** em lugar nenhum.
- **Desbloquear** a conta, sem trocar a senha.
- **Reativar no AD**, com senha nova opcional e a regra do SEI acima.

O motivo ou o número do chamado é obrigatório. A aba **Acessos e alterações** e o módulo **Logs** registram quem fez o quê, em qual conta, quando, de qual IP e por quê, sem a senha. Não dá para trocar a própria senha por aqui, nem mexer em contas protegidas do AD (administradores, `adminCount=1`).

**Requisitos no AD:**
1. **Uma conta com permissão para redefinir senhas.** No *Active Directory Users and Computers*, clique com o botão direito na OU dos usuários e escolha **Delegate Control…**. Adicione a conta e marque **Reset user passwords and force password change at next logon**. Para o desbloqueio, delegue também gravação em `lockoutTime`, em **Create a custom task → User objects → Write lockoutTime**. Use uma conta só para isso em `AD_RESET_USUARIO` e `AD_RESET_SENHA`, ou deixe vazio para usar a conta de serviço `AD_BIND_USUARIO`.
   Para o **Reativar no AD**, delegue também gravação em `userAccountControl` (**Create a custom task → User objects → Write userAccountControl**).
2. **Conexão criptografada com os DCs (LDAPS na porta 636 ou StartTLS).** O AD não aceita troca de senha sem criptografia. Os controladores de domínio precisam ter certificado, emitido por exemplo pela CA do AD (AD CS). Para testar, rode `docker exec -it gestao-ti-api python -m app.ad seu.login`: as linhas `ldaps` ou `starttls` precisam aparecer como OK.

## E-mails em lote

**Todos os e-mails saem da conta `geral@santaluzia.mg.gov.br`.** Ela é fixa no sistema: não há como trocar nem pela tela nem pelo `.env`.

**Modo de teste:** enquanto a `geral@` não estiver liberada no servidor de e-mail, ligue em Configurações → Conta de envio → **"Modo de teste: enviar por outra conta"** e informe um e-mail de teste, normalmente o mesmo do usuário de login. Com o modo ligado:
- todos os envios saem desse e-mail: disparos, e-mail de teste e senha de visitante;
- a tela de disparos mostra a conta com a etiqueta **"modo de teste"**;
- Configurações mostra um aviso no topo.

Desligue para voltar à `geral@`. Ligar e desligar fica registrado nos Logs. O nome do remetente e o "Responder para" continuam editáveis em cada disparo.

1. **Destinatários.**
   - **AD:** a lista de usuários ativos com e-mail, com busca, filtro por OU e seleção individual ou de todos os filtrados.
   - **TXT:** um e-mail por linha. Também aceita `Nome;email`, `email;Nome` e `Nome <email>`. Os repetidos são removidos, e as linhas inválidas aparecem para conferência.
2. **Mensagem.** Editor com formatação, variáveis `{nome}` e `{email}` e **Enviar teste para mim**.
   - **Imagens dentro da mensagem** (cartaz, banner, foto): use o botão **Imagem do computador**, cole com Ctrl+V ou arraste para o texto. Cada imagem pode ter até `IMAGEM_MAX_MB` e vai **embutida** no e-mail, então aparece mesmo para quem está fora da rede da prefeitura. Imagens coladas do Word, em base64, são convertidas sozinhas.
   - **Documentos e arquivos** (PDF, Word, Excel, imagens…): vão em **Anexos**, até `ANEXOS_MAX_MB` no total. Cada destinatário recebe uma cópia, então prefira arquivos leves em disparos grandes.
3. **Blocos.** Quantidade de blocos, ou tamanho de cada bloco. Modo **automático** (intervalo em minutos entre os blocos) ou **manual** (a pessoa clica em "Liberar próximo bloco").
4. **Revisar e enviar.**

**Remetente.** Cada pessoa só pode disparar com:
- o próprio e-mail do AD (atributo `mail`);
- os **aliases da própria conta**, guardados no AD em `proxyAddresses` (ex.: `smtp:geral@santaluzia.mg.gov.br`). Quem tem o alias na conta recebe os e-mails dele, então pode usá-lo como remetente;
- o remetente padrão da Gestão TI (módulo Configurações), **só para a equipe da TI**.

A lista é lida do AD na hora, então tirar o alias da conta no AD tira a permissão na hora. O servidor confere de novo ao criar o disparo e ao enviar teste. Para ler os aliases, precisa da conta de serviço (`AD_BIND_USUARIO`). Sem ela, só o próprio e-mail aparece. O servidor de e-mail precisa aceitar que a conta de envio configurada em Configurações dispare com esses endereços.

O envio roda em segundo plano, com uma pausa de `ENVIO_PAUSA_MS` entre um e-mail e outro. Na aba **Disparos** dá para acompanhar o progresso, pausar, retomar, cancelar, duplicar e baixar o relatório CSV, com a situação de cada destinatário. Se o servidor reiniciar, o envio continua de onde parou. Se o servidor de e-mail cair, o disparo é pausado e o motivo aparece na tela.

Quem é de fora da TI vê só os próprios disparos. A TI vê todos os disparos dos módulos que tem.

### Servidor de e-mail (produção)

O jeito mais simples é pelo módulo **Configurações**:
- preencha o servidor, a porta, a segurança, o usuário, a senha e o remetente. Há modelos prontos para servidor próprio, Microsoft 365 e Google;
- clique em **Testar conexão** e em **Enviar e-mail de teste**;
- clique em **Salvar**.

O que é salvo na tela vale no lugar do `.env`. A senha fica criptografada no banco e nunca volta para a tela. Cada alteração fica registrada em **Gestão de Usuários → Acessos e alterações** e no módulo **Logs**.

Se preferir, também dá para configurar pelo `.env`. Ele vale enquanto não houver nada salvo na tela:

```
SMTP_HOST=smtp.santaluzia.mg.gov.br
SMTP_PORT=587
SMTP_TLS=true          # ou SMTP_PORT=465 com SMTP_SSL=true
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM_NOME=Prefeitura Municipal de Santa Luzia
```

Confira o limite de envio do provedor. O Microsoft 365, por exemplo, aceita cerca de 10 mil destinatários por dia por caixa. Ajuste os blocos e o intervalo a esse limite.

---

# Módulo Wi-Fi no computador

O módulo controla quando um computador da prefeitura pode usar outras redes Wi-Fi além da **PMSL-WIRELESS**.

- **Portal do usuário** (`/wifi`): qualquer pessoa entra com o usuário e a senha do AD e pede a liberação do computador que está usando.
- **Módulo Wi-Fi no computador** (`/m/wifi`): a TI libera por um tempo em horas ou dias ou personalizado (até 30 dias), com bloqueio automático; libera por tempo indeterminado; bloqueia de novo; libera sem pedido; busca pessoas do AD; consulta e exporta o registro de atividades.
- **Script da GPO** (`wlan-filter.ps1`): roda como SYSTEM a cada 5 minutos, mantém o bloqueio e consulta a API.

## Sinal fraco e falta de conexão

O bloqueio só é ativado quando a PMSL-WIRELESS tem sinal utilizável (40% ou mais, como o Windows mostra). Depois de ativado, só é desfeito se o sinal cair abaixo de 30%, para não ficar ligando e desligando. Se o bloqueio estiver ativo e o computador passar duas execuções seguidas (até 10 minutos) sem conseguir conectar na PMSL-WIRELESS, o script libera as outras redes por 30 minutos e depois tenta de novo. Os valores ficam no início do `wlan-filter.ps1` (`$SinalMinimo`, `$MargemSinal`, `$ExecucoesSemConexao`, `$MinutosEsperaSemConexao`).

## Usuário logado em cada computador

O script da GPO também informa quem está logado na sessão física de cada máquina. A aba **Computadores** mostra esse usuário embaixo do nome do computador, com o nome do AD quando conhecido e o horário em que entrou. A busca da aba aceita o login ou o nome da pessoa ("em qual máquina o joaosilva está?"), e a aba **Pessoas** mostra **Logado agora em**.

Cada pedido guarda quem estava logado no computador na hora do pedido. Se for outra pessoa, o pedido aparece com a etiqueta **pedido por outra pessoa**. As entradas e saídas de usuário ficam no **Registro de atividades** ("Usuário logado no computador" e "Usuário saiu do computador").

Limitações: o dado acompanha o ciclo do script (até 5 minutos), não inclui sessões por acesso remoto (RDP) e, com a tela bloqueada, continua mostrando quem estava logado.

## Como o portal identifica o computador

O script da GPO informa ao servidor, a cada 5 minutos, o nome, o IP e o usuário logado de cada computador. Quando o usuário abre o portal, o servidor procura o computador nesta ordem: (1) pelo IP de quem está acessando; (2) pelo computador em que o login do AD está logado agora, o que funciona mesmo quando o navegador passa pelo proxy de navegação (se a pessoa estiver logada em mais de uma máquina, vale a do atalho); (3) pelo nome que vem no atalho (`?h=%ComputerName%`). Se nada disso resolver, o portal pede para o usuário digitar. Pedidos com nome digitado aparecem no painel com a etiqueta **nome digitado**.

## Instalação no servidor

```bash
# no servidor Linux, como root
cd /tmp && unzip -o gestao-ti.zip
cd /tmp/gestao-ti
chmod +x aplicar_gestao_ti.sh
PAINEL_URL=http://gestaoti.santaluzia.int:8090 ./aplicar_gestao_ti.sh
```

O script copia tudo para `/opt/gestao-ti`, cria o `.env` com senhas aleatórias, sobe os containers e gera `saida-windows/wlan-filter.ps1` com o endereço da API e a chave já preenchidos.

## Atualização (versões novas)

Repita os mesmos comandos, extraindo o zip novo em `/tmp`. O banco, os pedidos e o `.env` são mantidos. As configurações novas de cada versão são **acrescentadas** ao `.env`, sem alterar as que já existem.

## Dois controladores de domínio (failover)

O sistema usa os DCs de `AD_SERVIDORES` (10.1.100.2 e 10.1.100.3). Se um não responder, ele passa para o outro sozinho e deixa o que caiu de lado por 5 minutos, para não ficar esperando o tempo limite a cada login. Se o DC cair **no meio** de uma consulta (lista de usuários, busca, login de e-mail), a consulta é repetida no outro. Quando isso acontece, o log do container mostra `DC 10.1.100.x fora do ar: usando o outro servidor do AD`.

## Configurar o login pelo AD

As opções ficam em `/opt/gestao-ti/.env`. Depois de editar, aplique com `cd /opt/gestao-ti && docker compose up -d`.

| Opção | Para que serve |
| --- | --- |
| `AD_SERVIDORES` | Controladores de domínio, em ordem (`10.1.100.2,10.1.100.3`) |
| `AD_DOMINIO` / `AD_BASE_DN` | `santaluzia.int` / `DC=santaluzia,DC=int` |
| `AD_OU_PERMITIDA` | Quem entra no **painel**: contas dentro desta OU, incluindo as sub-OUs |
| `AD_GRUPO_PERMITIDO` | Alternativa ou complemento: DN de um grupo (grupos dentro de grupos contam) |
| `AD_SEGURANCA` | `auto` (tenta LDAPS 636 e depois StartTLS 389), `ldaps`, `starttls` ou `nenhuma` |
| `AD_PERMITIR_SEM_TLS` | `true` só se os DCs não tiverem certificado. A senha passa sem criptografia na rede interna |
| `AD_BIND_USUARIO` / `AD_BIND_SENHA` | Opcional: conta só de leitura para a aba **Pessoas** buscar em todo o AD. Sem ela, a busca mostra quem já usou o sistema |
| `HORAS_PERMITIDAS` / `DIAS_PERMITIDOS` | Opções de tempo do portal, em horas e em dias |
| `PERSONALIZADO_USUARIO` / `HORAS_MAXIMAS_USUARIO` | Libera o tempo personalizado no portal e define o máximo (720 = 30 dias) |
| `HORAS_ADMIN` / `DIAS_ADMIN` / `HORAS_MAXIMAS` | Opções de tempo do painel e o máximo que a TI pode liberar (720 = 30 dias) |
| `LOGIN_LOCAL_HABILITADO` | Mantém o usuário local `admin` como acesso de emergência se o AD cair |

**Para testar o AD**, sem precisar do navegador:

```bash
docker exec -it gestao-ti-api python -m app.ad seu.usuario
```

Ele pede a senha e mostra qual DC e qual modo (LDAPS, StartTLS ou sem criptografia) funcionaram, e se o usuário seria aceito no portal e no painel. Cada tentativa conta como uma tentativa de login no AD, então não teste com senha errada repetidas vezes.

## Distribuir pela GPO

1. Copie `/opt/gestao-ti/saida-windows/wlan-filter.ps1` para `\\santaluzia.int\NETLOGON\PMSL-WiFi`, depois de renomear o atual para `wlan-filter.ps1.anterior`:
   ```bash
   apt install -y smbclient
   cd /opt/gestao-ti/saida-windows
   smbclient //10.1.100.2/NETLOGON -U 'SANTA-LUZIA\seu.usuario' \
     -c 'cd PMSL-WiFi; rename wlan-filter.ps1 wlan-filter.ps1.anterior; put wlan-filter.ps1; ls'
   ```
2. No GPO **WiFi - Prioridade PMSL-WIRELESS**, crie o atalho do portal em **Computer Configuration → Preferences → Windows Settings → Shortcuts → New → Shortcut**:
   - **Action:** Replace
   - **Name:** `Solicitar Acesso à Rede Sem Fio`
   - **Target type:** URL
   - **Location:** All Users Start Menu (repita com **All Users Desktop** se quiser na área de trabalho)
   - **Target URL:** `http://wifi-aprovacao.santaluzia.int:8090/?h=%ComputerName%`
   - Aba **Common:** marque **Remove this item when it is no longer applied**

   O `%ComputerName%` é preenchido pela própria GPO em cada máquina.

## Marca

Para usar o logo da prefeitura, coloque `logo.svg` ou `logo.png` em `/opt/gestao-ti/marca/` e recarregue a página. Não é preciso reiniciar. Os textos (nome do sistema, órgão, rodapé) ficam no `.env` (`APP_NOME`, `APP_SUBTITULO`, `ORGAO_LINHA1`, `ORGAO_LINHA2`, `RODAPE`).

## Segurança

- **Ninguém se libera sozinho.** A liberação só acontece com uma decisão gravada pela TI. O portal só cria o pedido, com o login do AD de quem pediu.
- **Chave do agente (`AGENT_KEY`).** Ela fica no script do NETLOGON, que todos os usuários do domínio conseguem ler. Com ela, alguém só consegue consultar se um computador está liberado e informar nome e IP. Não consegue liberar nada.
- **Login no painel.** Há bloqueio de 10 minutos depois de 5 senhas erradas no mesmo IP. O registro guarda todas as tentativas.
- **IP do usuário.** O IP vem da conexão e não pode ser falsificado por cabeçalho. Se um dia o sistema ficar atrás de um proxy reverso, coloque o IP do proxy em `FORWARDED_ALLOW_IPS`.
- **Se a API cair**, o script segue a regra normal: bloqueia na prefeitura e libera fora dela.

## Manutenção

- **Logs:** `cd /opt/gestao-ti && docker compose logs -f api`
- **Backup:**
  ```bash
  docker exec gestao-ti-db pg_dump -U gestao gestao_ti > backup_gestao_ti_$(date +%F).sql
  ```
- **Senha do `admin` local esquecida:**
  ```bash
  docker exec -it gestao-ti-api python -c "from app.auth import gerar_hash; from app.db import *; s=SessionLocal(); a=s.query(Administrador).filter_by(usuario='admin').one(); a.senha_hash=gerar_hash('NovaSenha123'); a.ativo=True; s.commit()"
  ```

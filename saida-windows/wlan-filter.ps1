# wlan-filter.ps1 - Mostra so a PMSL-WIRELESS quando ela estiver ao alcance
# Versao com liberacao temporaria aprovada pela TI (API de aprovacao).
# Executado pela tarefa agendada "PMSL - Filtro WiFi" como SYSTEM (pode rodar a cada 1 minuto).
#
# - A API e consultada em toda execucao (liberacao/bloqueio chegam rapido).
# - A varredura forcada de redes so acontece a cada $MinutosVarredura minutos,
#   ou quando o estado muda (evita pico de CPU e oscilacao no Wi-Fi).
# - O log so recebe uma linha quando o estado muda. A hora da ultima execucao
#   fica em C:\ProgramData\PMSL\ultima-execucao.txt.

$SSID  = 'PMSL-WIRELESS'
$Pasta = 'C:\ProgramData\PMSL'
$Log   = Join-Path $Pasta 'wlan-filter.log'
$MinutosVarredura = 5

# Sinal fraco: so bloqueia as outras redes se a PMSL-WIRELESS tiver sinal utilizavel
$SinalMinimo = 40          # % de sinal (como o Windows mostra) para ativar o bloqueio
$MargemSinal = 10          # depois de bloqueado, so libera se cair abaixo de (SinalMinimo - MargemSinal), evita liga/desliga
# Sem conexao: se o bloqueio estiver ativo e o computador nao conseguir conectar na PMSL-WIRELESS
$ExecucoesSemConexao = 2   # execucoes seguidas sem conexao (2 x 5 min = ate 10 min) antes de liberar
$MinutosEsperaSemConexao = 30   # tempo que fica liberado antes de tentar bloquear de novo
# API sem resposta com o computador liberado: mantem a liberacao por ate N minutos desde a ultima resposta
# (evita bloquear por uma falha rapida de DNS ou uma reinicializacao do servidor)
$MinutosToleranciaApi = 30

# Endereco da API e chave do agente (preenchidos pelo aplicar_gestao_ti.sh)
$ApiUrl   = 'http://wifiaprovacao.santaluzia.int:8090'
$AgentKey = 'Wa3dPx6nmEheVTOEcKGanELtHqc82IvRe1TKambNJNcbXiQY'

if (-not (Test-Path $Pasta)) { New-Item -ItemType Directory -Path $Pasta -Force | Out-Null }
Set-Content -Path (Join-Path $Pasta 'ultima-execucao.txt') -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -Encoding utf8

function Registrar($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -FilePath $Log -Append -Encoding utf8
    # Mantem o log com no maximo 500 linhas
    $linhas = Get-Content $Log -ErrorAction SilentlyContinue
    if ($linhas.Count -gt 500) { $linhas | Select-Object -Last 500 | Set-Content $Log -Encoding utf8 }
}

# Estado da execucao anterior (bloqueado, liberado, fora do alcance...)
$ArqEstado = Join-Path $Pasta 'estado.txt'
$EstadoAnterior = "$(Get-Content $ArqEstado -Raw -ErrorAction SilentlyContinue)".Trim()

function Registrar-Estado($estado, $msg) {
    # Grava no log so quando o estado muda em relacao a execucao anterior
    if ($estado -ne $script:EstadoAnterior) {
        Set-Content -Path $ArqEstado -Value $estado -Encoding utf8
        Registrar $msg
    }
}

function Liberar-Todas {
    netsh wlan delete filter permission=denyall networktype=infrastructure 2>&1 | Out-Null
    netsh wlan delete filter permission=allow ssid="$SSID" networktype=infrastructure 2>&1 | Out-Null
}

$cs = @'
using System; using System.Runtime.InteropServices;
public static class WlanScanner {
    [DllImport("wlanapi.dll")] static extern uint WlanOpenHandle(uint v, IntPtr r, out uint n, out IntPtr h);
    [DllImport("wlanapi.dll")] static extern uint WlanScan(IntPtr h, ref Guid g, IntPtr s, IntPtr ie, IntPtr r);
    [DllImport("wlanapi.dll")] static extern uint WlanCloseHandle(IntPtr h, IntPtr r);
    public static void Scan(Guid g) {
        uint n; IntPtr h;
        if (WlanOpenHandle(2, IntPtr.Zero, out n, out h) == 0) {
            WlanScan(h, ref g, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
            WlanCloseHandle(h, IntPtr.Zero);
        }
    }
}
'@
$ArqVarredura = Join-Path $Pasta 'ultima-varredura.txt'
function Forcar-Varredura([switch]$Sempre) {
    # Sem -Sempre, so varre se a ultima varredura tiver mais de $MinutosVarredura minutos
    if (-not $Sempre) {
        $ultima = "$(Get-Content $ArqVarredura -Raw -ErrorAction SilentlyContinue)".Trim()
        $ticks = 0L
        if ([long]::TryParse($ultima, [ref]$ticks)) {
            $minutos = ([DateTime]::UtcNow - [DateTime]::new($ticks, [DateTimeKind]::Utc)).TotalMinutes
            if ($minutos -ge 0 -and $minutos -lt ($MinutosVarredura - 0.5)) { return }
        }
    }
    try {
        if (-not ('WlanScanner' -as [type])) { Add-Type -TypeDefinition $cs -ErrorAction Stop }
        Get-NetAdapter -Physical -ErrorAction Stop |
            Where-Object { $_.NdisPhysicalMedium -eq 9 -and $_.Status -ne 'Disabled' } |
            ForEach-Object { [WlanScanner]::Scan([Guid]$_.InterfaceGuid) }
        Set-Content -Path $ArqVarredura -Value ([DateTime]::UtcNow.Ticks) -Encoding utf8
        Start-Sleep -Seconds 5
    } catch {
        Registrar "Aviso: nao foi possivel forcar a varredura ($($_.Exception.Message))"
    }
}

function Atualizar-Lista-Redes($motivo) {
    # A lista de redes do icone do Wi-Fi so se atualiza de verdade quando a placa reinicia
    # (o mesmo que desativar e ativar o Wi-Fi). A conexao cai por alguns segundos e volta
    # sozinha, sem pedir senha. Acontece so quando a TI libera ou bloqueia (e quando o prazo acaba).
    try {
        $placas = @(Get-NetAdapter -Physical -ErrorAction Stop |
            Where-Object { $_.NdisPhysicalMedium -eq 9 -and $_.Status -ne 'Disabled' })
        foreach ($p in $placas) { Restart-NetAdapter -Name $p.Name -Confirm:$false -ErrorAction Stop }
        if ($placas.Count -gt 0) {
            Registrar "Placa Wi-Fi reiniciada para atualizar a lista de redes ($motivo): $(($placas | ForEach-Object Name) -join ', ')"
            Start-Sleep -Seconds 8   # tempo para a placa voltar e reconectar
        }
    } catch {
        Registrar "Aviso: nao foi possivel reiniciar a placa Wi-Fi ($($_.Exception.Message))"
    }
}

# 0. Interruptor geral: se existir PARAR_GPO.txt no NETLOGON, libera todas as redes e sai
$Parar = '\\santaluzia.int\NETLOGON\PMSL-WiFi\PARAR_GPO.txt'
if (Test-Path $Parar) {
    Liberar-Todas
    Registrar-Estado 'parado' "PARADO pela TI (PARAR_GPO.txt no NETLOGON) - todas as redes liberadas"
    exit 0
}

# 0b. Liberacao aprovada pela TI para ESTE computador
#     Se a API estiver fora do ar ou o computador fora da rede, segue a regra normal.
$EstadoApi = Join-Path $Pasta 'api-estado.txt'
function Registrar-Api($texto) {
    # Registra no log so quando a situacao da API muda (evita repetir a cada 5 minutos)
    $anterior = (Get-Content $EstadoApi -Raw -ErrorAction SilentlyContinue)
    if ("$anterior".Trim() -ne $texto) { Set-Content -Path $EstadoApi -Value $texto -Encoding utf8; Registrar $texto }
}

function Usuario-Logado {
    # Quem esta na sessao fisica do computador (ex.: SANTA-LUZIA\joaosilva). Vazio = ninguem logado.
    $u = $null
    try { $u = (Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName } catch {}
    if (-not $u) {
        # Alternativa: dono do explorer.exe (cobre alguns casos em que o UserName vem vazio)
        try {
            $dono = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop |
                Select-Object -First 1 | Invoke-CimMethod -MethodName GetOwner -ErrorAction Stop
            if ($dono.User) { $u = "$($dono.Domain)\$($dono.User)" }
        } catch {}
    }
    return "$u"
}

function Consultar-Liberacao {
    $url = "$($ApiUrl.TrimEnd('/'))/api/agente/liberacao?hostname=$([uri]::EscapeDataString($env:COMPUTERNAME))" +
           "&usuario=$([uri]::EscapeDataString((Usuario-Logado)))"
    $req = [System.Net.HttpWebRequest]::Create($url)
    $req.Method = 'GET'
    $req.Timeout = 8000
    $req.ReadWriteTimeout = 8000
    $req.Proxy = $null          # fala direto com o servidor, sem passar pelo proxy da rede
    $req.Headers.Add('X-Agent-Key', $AgentKey)
    $resp = $req.GetResponse()
    try {
        $leitor = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        return ($leitor.ReadToEnd() | ConvertFrom-Json)
    } finally { $resp.Close() }
}

# Pedidos da TI para este computador (ex.: encerrar a sessao de um usuario no Windows)
function Enviar-Resultado($id, $resultado, $detalhe) {
    try {
        $url = "$($ApiUrl.TrimEnd('/'))/api/agente/comando/$id/resultado?hostname=$([uri]::EscapeDataString($env:COMPUTERNAME))" +
               "&resultado=$([uri]::EscapeDataString($resultado))&detalhe=$([uri]::EscapeDataString("$detalhe"))"
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Method = 'GET'; $req.Timeout = 8000; $req.ReadWriteTimeout = 8000; $req.Proxy = $null
        $req.Headers.Add('X-Agent-Key', $AgentKey)
        $req.GetResponse().Close()
    } catch {
        Registrar "Aviso: nao foi possivel informar o resultado do pedido #$id ($($_.Exception.Message))"
    }
}

function Sessoes-Do-Usuario($login) {
    # IDs das sessoes do Windows deste usuario (quser mostra o login sem o dominio, com ate 20 caracteres)
    $alvo = ($login.ToLower() -split '\\')[-1]
    if ($alvo.Length -gt 20) { $alvo = $alvo.Substring(0, 20) }
    $ids = @()
    $saida = @(& "$env:windir\System32\quser.exe" 2>$null)
    foreach ($l in ($saida | Select-Object -Skip 1)) {
        if ($l -match '^\s*>?(\S+)\s+(?:(\S+)\s+)?(\d+)\s+\S+') {
            if ($Matches[1].ToLower() -eq $alvo) { $ids += [int]$Matches[3] }
        }
    }
    return $ids
}

function Executar-Comandos($comandos) {
    foreach ($c in @($comandos)) {
        if ($c.acao -ne 'logoff') { Enviar-Resultado $c.id 'erro' "acao desconhecida: $($c.acao)"; continue }
        try {
            $ids = @(Sessoes-Do-Usuario $c.usuario)
            if (-not $ids.Count) {
                Registrar "Pedido #$($c.id) da TI: logoff de $($c.usuario), mas ele nao estava logado"
                Enviar-Resultado $c.id 'nao_logado' ''
                continue
            }
            $espera = [int]$c.avisar_segundos
            if ($espera -gt 0) {
                foreach ($id in $ids) {
                    & "$env:windir\System32\msg.exe" $id "/TIME:$espera" ("A TI da Prefeitura vai encerrar a sua sessão do Windows em " +
                        "$espera segundos. Salve o que estiver fazendo agora.") 2>$null | Out-Null
                }
                Start-Sleep -Seconds $espera
            }
            foreach ($id in @(Sessoes-Do-Usuario $c.usuario)) { & "$env:windir\System32\logoff.exe" $id 2>$null | Out-Null }
            Start-Sleep -Seconds 3
            if (@(Sessoes-Do-Usuario $c.usuario).Count) {
                Registrar "Pedido #$($c.id) da TI: logoff de $($c.usuario) NAO concluido (a sessao continua aberta)"
                Enviar-Resultado $c.id 'erro' 'a sessao continua aberta depois do logoff'
            } else {
                Registrar "Pedido #$($c.id) da TI: logoff de $($c.usuario) feito"
                Enviar-Resultado $c.id 'ok' ''
            }
        } catch {
            Registrar "Pedido #$($c.id) da TI: erro no logoff de $($c.usuario) ($($_.Exception.Message))"
            Enviar-Resultado $c.id 'erro' $_.Exception.Message
        }
    }
}

if ($ApiUrl -and $ApiUrl -notlike '__*') {
    $r = $null
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $r = Consultar-Liberacao
        Registrar-Api "API de liberacao respondendo normalmente ($ApiUrl)"
    } catch {
        Registrar-Api "Aviso: API de liberacao sem resposta em $ApiUrl ($($_.Exception.Message)) - seguindo a regra normal"
    }
    if ($r -and $r.comandos) { Executar-Comandos $r.comandos }
    $ArqApiOk = Join-Path $Pasta 'api-ultima-resposta.txt'
    $ArqLiberadoAte = Join-Path $Pasta 'liberado-ate.txt'
    if ($r) {
        Set-Content -Path $ArqApiOk -Value ([DateTime]::UtcNow.Ticks) -Encoding utf8
    } elseif ($EstadoAnterior -like 'liberado*') {
        # API sem resposta e o computador estava liberado: mantem a liberacao por ate $MinutosToleranciaApi minutos
        # desde a ultima resposta, respeitando o prazo da liberacao
        $ticks = 0L
        [void][long]::TryParse("$(Get-Content $ArqApiOk -Raw -ErrorAction SilentlyContinue)".Trim(), [ref]$ticks)
        $minutos = if ($ticks -gt 0) { ([DateTime]::UtcNow - [DateTime]::new($ticks, [DateTimeKind]::Utc)).TotalMinutes } else { 999999 }
        $ate = "$(Get-Content $ArqLiberadoAte -Raw -ErrorAction SilentlyContinue)".Trim()
        $dentroDoPrazo = $true
        if ($ate -and $ate -ne 'indeterminado') {
            try { $dentroDoPrazo = ([DateTimeOffset]::Parse($ate) -gt [DateTimeOffset]::Now) } catch { $dentroDoPrazo = $true }
        }
        if ($minutos -ge 0 -and $minutos -lt $MinutosToleranciaApi -and $dentroDoPrazo) {
            Liberar-Todas
            exit 0
        }
    }
    if ($r -and $r.liberado) {
        Set-Content -Path $ArqLiberadoAte -Encoding utf8 -Value $(if ($r.indeterminado -or -not $r.valido_ate) { 'indeterminado' } else { "$($r.valido_ate)" })
        Liberar-Todas
        if ($r.indeterminado -or -not $r.valido_ate) {
            $prazo = 'por tempo indeterminado (ate a TI bloquear)'
        } else {
            try { $prazo = 'ate ' + ([DateTimeOffset]::Parse($r.valido_ate)).ToLocalTime().ToString('dd/MM/yyyy HH:mm') }
            catch { $prazo = "ate $($r.valido_ate)" }
        }
        $estado = "liberado #$($r.solicitacao_id) $prazo"
        if ($estado -ne $EstadoAnterior) {
            Registrar-Estado $estado "LIBERADO pela TI (solicitacao #$($r.solicitacao_id)) $prazo - todas as redes liberadas"
            Forcar-Varredura -Sempre   # atualiza a lista de redes para as outras aparecerem logo
            if ($EstadoAnterior -notlike 'liberado*') { Atualizar-Lista-Redes 'liberacao' }
        }
        exit 0
    }
}

# 1. Garante o allow da rede da prefeitura (erro de "ja existe" e ignorado)
netsh wlan add filter permission=allow ssid="$SSID" networktype=infrastructure 2>&1 | Out-Null

# 2. Pede ao Windows uma nova varredura de redes (sem isso a lista pode estar desatualizada).
#    Logo apos o fim de uma liberacao varre sempre; nas demais execucoes, a cada $MinutosVarredura minutos.
if ($EstadoAnterior -like 'liberado*' -or $EstadoAnterior -eq 'parado' -or -not $EstadoAnterior) {
    if ($EstadoAnterior -like 'liberado*') { Registrar "Liberacao encerrada (prazo vencido ou bloqueada pela TI) - voltando a regra normal" }
    Forcar-Varredura -Sempre
} else {
    Forcar-Varredura
}

# 3. Le a lista de redes ao alcance, com a forca do sinal (exige localizacao ativa no Windows 11 24H2+)
$linhas = @(netsh wlan show networks mode=bssid)
if ($LASTEXITCODE -ne 0) {
    # Sem acesso a lista: libera as redes para o usuario nao ficar sem conexao
    netsh wlan delete filter permission=denyall networktype=infrastructure 2>&1 | Out-Null
    Registrar-Estado 'erro' "ERRO: sem acesso a lista de redes (verificar localizacao) - bloqueio DESATIVADO por seguranca"
    exit 1
}

function Sinal-Da-Rede($saida) {
    # Maior sinal (%) entre os pontos de acesso da PMSL-WIRELESS; -1 se ela nao estiver ao alcance.
    # Funciona com o Windows em portugues (Sinal) ou ingles (Signal).
    $melhor = -1; $naRede = $false
    foreach ($l in $saida) {
        if ($l -match '^\s*SSID \d+\s*:\s*(.*?)\s*$') { $naRede = ($Matches[1] -eq $SSID); if ($naRede -and $melhor -lt 0) { $melhor = 0 }; continue }
        if ($naRede -and $l -match '^\s*(Signal|Sinal)\s*:\s*(\d+)\s*%') { $melhor = [Math]::Max($melhor, [int]$Matches[2]) }
    }
    return $melhor
}
function Liberar-Redes-Regra($estado, $msg) {
    netsh wlan delete filter permission=denyall networktype=infrastructure 2>&1 | Out-Null
    $mudou = ($estado -ne $EstadoAnterior)
    Registrar-Estado $estado $msg
    # Saindo do bloqueio: reinicia a placa para as outras redes aparecerem na lista na hora
    $motivo = @{ fraco = 'sinal fraco'; fora = 'fora do alcance'; sem_conexao = 'sem conexao na rede da prefeitura' }[($estado -split '\|')[0]]
    if ($mudou -and $EstadoAnterior -eq 'bloqueado') { Atualizar-Lista-Redes $motivo }
}

$sinal = Sinal-Da-Rede $linhas
$ArqSemConexao = Join-Path $Pasta 'sem-conexao.txt'

# 3b. Liberado ha pouco por falta de conexao: espera antes de tentar bloquear de novo
if ($EstadoAnterior -like 'sem_conexao|*') {
    $ticks = 0L
    [void][long]::TryParse(($EstadoAnterior -split '\|')[1], [ref]$ticks)
    $minutos = ([DateTime]::UtcNow - [DateTime]::new($ticks, [DateTimeKind]::Utc)).TotalMinutes
    if ($sinal -ge 0 -and $minutos -ge 0 -and $minutos -lt $MinutosEsperaSemConexao) {
        netsh wlan delete filter permission=denyall networktype=infrastructure 2>&1 | Out-Null
        exit 0
    }
}

# 4. Liga ou desliga o bloqueio das demais redes
$minimo = if ($EstadoAnterior -eq 'bloqueado') { $SinalMinimo - $MargemSinal } else { $SinalMinimo }
if ($sinal -ge $minimo) {
    # Bloqueio comecando agora (fim de liberacao, rede voltou ao alcance, sinal voltou...)
    $inicioBloqueio = ($EstadoAnterior -ne 'bloqueado')
    netsh wlan add filter permission=denyall networktype=infrastructure 2>&1 | Out-Null
    Registrar-Estado 'bloqueado' "$SSID ao alcance (sinal $sinal%) - bloqueio das outras redes ATIVADO"
    # Sem reiniciar a placa (o reinicio fazia o Windows voltar no primeiro ponto de acesso que achasse, com sinal
    # fraco, ate trocar sozinho minutos depois): uma varredura nova tira as outras redes da lista, e quem ja esta
    # na PMSL-WIRELESS nao perde a conexao.
    if ($inicioBloqueio) { Forcar-Varredura -Sempre }

    $atual = @(netsh wlan show interfaces) -match '^\s*SSID\s*:' | ForEach-Object { ($_ -split ':', 2)[1].Trim() }
    if ($atual -eq $SSID) {
        Remove-Item $ArqSemConexao -ErrorAction SilentlyContinue
    } else {
        # Conectado em outra rede (ou sem conexao depois do filtro): troca direto para a rede da prefeitura.
        # O Windows escolhe o ponto de acesso de melhor sinal pela varredura que acabou de ser feita.
        if ($atual) {
            netsh wlan disconnect 2>&1 | Out-Null
            Start-Sleep -Seconds 2
            Registrar "Estava conectado em '$atual' - trocando para $SSID"
        }
        netsh wlan connect name="$SSID" 2>&1 | Out-Null
        if ($inicioBloqueio) {
            # A conexao ainda esta sendo feita: so comeca a contar "sem conexao" a partir da proxima execucao
            Remove-Item $ArqSemConexao -ErrorAction SilentlyContinue
            exit 0
        }
        # Conta as execucoes seguidas em que o computador ficou sem conexao na PMSL-WIRELESS
        $n = 0; [void][int]::TryParse("$(Get-Content $ArqSemConexao -Raw -ErrorAction SilentlyContinue)".Trim(), [ref]$n)
        $n++
        if ($n -ge $ExecucoesSemConexao) {
            Remove-Item $ArqSemConexao -ErrorAction SilentlyContinue
            Liberar-Redes-Regra "sem_conexao|$([DateTime]::UtcNow.Ticks)" ("Bloqueio ativo mas sem conseguir conectar na $SSID (sinal $sinal%) - " +
                "bloqueio DESATIVADO por $MinutosEsperaSemConexao minutos")
        } else {
            Set-Content -Path $ArqSemConexao -Value $n -Encoding utf8
        }
    }
} elseif ($sinal -ge 0) {
    Remove-Item $ArqSemConexao -ErrorAction SilentlyContinue
    Liberar-Redes-Regra 'fraco' "$SSID com sinal fraco ($sinal%) - bloqueio das outras redes DESATIVADO"
} else {
    Remove-Item $ArqSemConexao -ErrorAction SilentlyContinue
    Liberar-Redes-Regra 'fora' "$SSID fora do alcance - bloqueio das outras redes DESATIVADO"
}

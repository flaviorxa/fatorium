CREATE TABLE IF NOT EXISTS ativos (
    ticker TEXT PRIMARY KEY,
    nome TEXT,
    classe TEXT,
    setor TEXT,
    moeda TEXT,
    fonte TEXT,
    atualizado_em TEXT,
    ativo INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cotacoes (
    ticker TEXT NOT NULL,
    data TEXT NOT NULL,
    abertura REAL,
    maxima REAL,
    minima REAL,
    fechamento REAL,
    fechamento_ajustado REAL,
    volume REAL,
    fonte TEXT,
    coletado_em TEXT,
    PRIMARY KEY (ticker, data)
);

CREATE TABLE IF NOT EXISTS fundamentos (
    ticker TEXT NOT NULL,
    data_ref TEXT NOT NULL,
    campo TEXT NOT NULL,
    valor REAL,
    fonte TEXT,
    coletado_em TEXT,
    PRIMARY KEY (ticker, data_ref, campo)
);

CREATE TABLE IF NOT EXISTS proventos (
    ticker TEXT NOT NULL,
    data_ex TEXT NOT NULL,
    valor REAL NOT NULL,
    tipo TEXT NOT NULL,
    fonte TEXT,
    coletado_em TEXT,
    PRIMARY KEY (ticker, data_ex, tipo, valor)
);

CREATE TABLE IF NOT EXISTS coletas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iniciado_em TEXT,
    terminado_em TEXT,
    tipo TEXT,
    status TEXT,
    resumo_json TEXT
);

CREATE TABLE IF NOT EXISTS falhas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quando TEXT,
    fonte TEXT,
    ticker TEXT,
    etapa TEXT,
    erro TEXT
);

CREATE TABLE IF NOT EXISTS configuracoes (
    chave TEXT PRIMARY KEY,
    valor TEXT,
    valor_json TEXT,
    atualizado_em TEXT
);

CREATE TABLE IF NOT EXISTS rankings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estrategia TEXT NOT NULL,
    gerado_em TEXT,
    config_json TEXT,
    universo_n INTEGER
);

CREATE TABLE IF NOT EXISTS ranking_itens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ranking_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    posicao INTEGER,
    pontuacao REAL,
    dados_json TEXT,
    motivos_json TEXT,
    fora_da_lista INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (ranking_id) REFERENCES rankings (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alertas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em TEXT NOT NULL,
    tipo TEXT NOT NULL,
    ticker TEXT,
    estrategia TEXT,
    titulo TEXT NOT NULL,
    motivo TEXT,
    data_ref TEXT,
    lido INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tipo, ticker, estrategia, data_ref)
);

CREATE TABLE IF NOT EXISTS rankings_geracoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iniciado_em TEXT,
    terminado_em TEXT,
    status TEXT,
    resumo_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_cotacoes_ticker_data ON cotacoes (ticker, data);
CREATE INDEX IF NOT EXISTS idx_fundamentos_ticker_data ON fundamentos (ticker, data_ref);
CREATE INDEX IF NOT EXISTS idx_falhas_fonte ON falhas (fonte);
CREATE INDEX IF NOT EXISTS idx_rankings_estrategia ON rankings (estrategia, id);
CREATE INDEX IF NOT EXISTS idx_ranking_itens_ranking ON ranking_itens (ranking_id);
CREATE INDEX IF NOT EXISTS idx_alertas_lido ON alertas (lido, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alertas_unico
    ON alertas (
        tipo,
        COALESCE(ticker, ''),
        COALESCE(estrategia, ''),
        COALESCE(data_ref, ''),
        COALESCE(titulo, '')
    );

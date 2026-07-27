-- ============================================================================
-- Damco SEO AI Agent System — tenant profile (migration 012)
--
-- Purpose
-- -------
-- Agent code should not know whose site it is running for. Before this
-- migration, tenant identity lived in Python constants scattered across the
-- agent folders: owned-domain sets in three files, two competing offering
-- vocabularies, a hardcoded sitemap table, thresholds tuned to one property,
-- and brand names interpolated directly into LLM prompts.
--
-- This migration gives that data a home. Nothing here changes behaviour: the
-- seed below reproduces the values the code holds today, so the first run
-- after adoption must produce identical output. The constants come out of the
-- code in the migrations and commits that follow.
--
-- Scope decision — one database per client
-- ----------------------------------------
-- There is deliberately NO `tenant_id` foreign key on the 29 operational
-- tables. Retrofitting one across ~180,000 rows solves a partitioning problem
-- that does not exist yet: this system runs one client per database. The
-- profile tables below hold exactly one tenant row, and `active_tenant()`
-- resolves it. When a second client genuinely needs to share a database,
-- nothing designed here has to change — the loader gains a slug argument it
-- already accepts.
--
-- What belongs here, and what does not
-- ------------------------------------
-- Tenant/vertical data belongs here: brand identity, owned domains, service
-- lines, the vocabularies that describe this client's market, and thresholds
-- calibrated to this client's site.
--
-- Language-level data does not. English stopwords and prose markers in
-- `trend_scout` are a property of English, not of Damco, and moving them here
-- would imply a multi-language capability this system does not have.
--
-- Idempotent. Safe to re-run; seeds use ON CONFLICT DO NOTHING so operator
-- edits made after the first apply are never clobbered.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Core identity.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
    id                  SERIAL PRIMARY KEY,
    slug                TEXT        NOT NULL UNIQUE,
    brand_name          TEXT        NOT NULL,
    legal_name          TEXT,
    primary_domain      TEXT        NOT NULL,

    -- Free-text descriptors injected into LLM system prompts. These replace
    -- phrases like "Damco Group (B2B IT services and AI consulting)" that
    -- were hardcoded into five separate prompt strings.
    vertical            TEXT,
    audience_descriptor TEXT,

    -- Market. DataForSEO defaults live in .env today and are process-wide;
    -- holding them per tenant is what eventually allows a UK and a US client
    -- to be tracked from one deployment.
    location_code       INTEGER     NOT NULL DEFAULT 2840,
    language_code       TEXT        NOT NULL DEFAULT 'en',
    device              TEXT        NOT NULL DEFAULT 'desktop'
                        CHECK (device IN ('desktop', 'mobile', 'tablet')),
    currency            TEXT        NOT NULL DEFAULT 'USD',
    timezone            TEXT        NOT NULL DEFAULT 'UTC',

    -- Identity we present to other people's servers. The crawler and the
    -- sitemap walker each carried their own copy of a "DamcoSEOBot" string,
    -- which was being sent to competitors and to every RSS host we poll.
    crawler_bot_name    TEXT        NOT NULL DEFAULT 'SEOBot',
    crawler_contact_url TEXT,

    -- Default link target for outreach and guest-post CTAs.
    cta_url             TEXT,

    status              TEXT        NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'inactive')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE tenants IS
    'One row per client. Agent code reads this instead of hardcoding identity.';


-- ---------------------------------------------------------------------------
-- Owned domains. Replaces BRAND_DOMAINS (rank_tracker), DAMCO_DOMAINS
-- (platform_finder) and the DOMAINS table (sitemap_validator) — three
-- independent copies that could drift apart, and had.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenant_domains (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    domain      TEXT        NOT NULL,
    role        TEXT        NOT NULL DEFAULT 'sister'
                CHECK (role IN ('primary', 'sister', 'staging')),

    -- NULL means "discover it". common/sitemap.py already has a fully generic
    -- discover_sitemap_urls() that probes six conventional locations and reads
    -- robots.txt; sitemap_validator just never called it.
    sitemap_url TEXT,

    -- Some properties use www and some do not. Recording it explicitly stops
    -- the link graph fragmenting on strict origin equality.
    uses_www    BOOLEAN     NOT NULL DEFAULT FALSE,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, domain)
);


-- ---------------------------------------------------------------------------
-- Service lines. `keywords.offering` is the operational source of truth for
-- which offerings exist; this table adds the token vocabularies that the
-- classifiers need and the DB cannot infer.
--
-- Two hardcoded copies existed. trend_scout's 15 keys matched keywords.offering
-- exactly. platform_finder's 14 keys ("Achieva", "OutSystems", "Staffing", ...)
-- matched NONE of them — that dict had drifted so far it was scoring every
-- candidate at niche relevance 0.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenant_offerings (
    id           SERIAL PRIMARY KEY,
    tenant_id    INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,     -- must match keywords.offering
    slug         TEXT,

    -- Phrases that identify this offering in free text. Longest match wins,
    -- so multi-word entries ("power bi") beat single tokens ("bi").
    tokens       TEXT[]      NOT NULL DEFAULT '{}',

    -- Shorter, domain-name-shaped tokens for matching publications and
    -- competitor hostnames. Falls back to `tokens` when empty.
    niche_tokens TEXT[]      NOT NULL DEFAULT '{}',

    sort_order   INTEGER     NOT NULL DEFAULT 100,
    status       TEXT        NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'inactive')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);


-- ---------------------------------------------------------------------------
-- Named term lists. One row per term so operators can add or retire a single
-- entry without rewriting a Python literal.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenant_vocabularies (
    id         SERIAL PRIMARY KEY,
    tenant_id  INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind       TEXT        NOT NULL,
    term       TEXT        NOT NULL,

    -- Only some kinds use these. `weight` carries the signal strength of a
    -- glossary definition pattern; `label` carries the page_type a URL path
    -- maps to.
    weight     NUMERIC(4,2),
    label      TEXT,
    notes      TEXT,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, kind, term)
);

COMMENT ON COLUMN tenant_vocabularies.kind IS
    'commercial_tokens | generic_heads | domain_blacklist | big_tech_domains | '
    'aggregator_domains | informational_domains | service_keywords | '
    'url_path_map | analyst_sources | banned_claims | banned_openers';

CREATE INDEX IF NOT EXISTS idx_tenant_vocab_kind
    ON tenant_vocabularies (tenant_id, kind) WHERE enabled;


-- ---------------------------------------------------------------------------
-- Tuned numbers. Every value here was a Python constant presented as though
-- it were universal. The CWV marks are the clearest example: 60/85 are this
-- client's own baseline, not Google's 90 — another site scoring 62 would
-- "pass" here while genuinely failing Core Web Vitals.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenant_policies (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key         TEXT        NOT NULL,
    value       JSONB       NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key)
);


-- ---------------------------------------------------------------------------
-- Resolver. One active tenant per database; the loader calls this when no
-- slug is supplied.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION active_tenant() RETURNS INTEGER AS $$
    SELECT id FROM tenants WHERE status = 'active' ORDER BY id LIMIT 1;
$$ LANGUAGE sql STABLE;


-- ============================================================================
-- Seed — reproduces the values currently hardcoded in Python.
-- ============================================================================

INSERT INTO tenants (
    slug, brand_name, legal_name, primary_domain,
    vertical, audience_descriptor,
    location_code, language_code, device,
    crawler_bot_name, crawler_contact_url, cta_url
) VALUES (
    'damco',
    'Damco Group',
    'Damco Solutions',          -- guest_post_drafter's BRAND_NAME said
                                -- "Damco Solutions" while every other module
                                -- said "Damco Group". Both preserved rather
                                -- than silently picking one.
    'damcogroup.com',
    'B2B IT services and AI consulting',
    'CIOs, CTOs, and IT decision-makers',
    2840, 'en', 'desktop',
    'DamcoSEOBot', 'https://www.damcogroup.com/',
    'https://www.damcogroup.com/'
) ON CONFLICT (slug) DO NOTHING;


INSERT INTO tenant_domains (tenant_id, domain, role, sitemap_url, uses_www) VALUES
    (active_tenant(), 'damcogroup.com',   'primary', 'https://www.damcogroup.com/sitemap.xml',       TRUE),
    (active_tenant(), 'damcodigital.com', 'sister',  'https://damcodigital.com/sitemap_index.xml',   FALSE),
    (active_tenant(), 'achieva.ai',       'sister',  'https://achieva.ai/sitemap.xml',               FALSE)
ON CONFLICT (tenant_id, domain) DO NOTHING;


-- Offerings: names taken from keywords.offering (the 15 that actually exist),
-- token vocabularies lifted verbatim from trend_scout.OFFERING_TOKENS.
INSERT INTO tenant_offerings (tenant_id, name, slug, tokens, niche_tokens, sort_order) VALUES
    (active_tenant(), 'AI', 'ai', ARRAY[
        'ai','artificial intelligence','machine learning','genai','generative ai','llm',
        'large language model','agentic','agentic ai','ai agent','copilot','rag',
        'retrieval augmented','prompt','foundation model','mlops','computer vision','nlp',
        'deep learning','fine-tuning','inference','vector database','embedding'],
        ARRAY['ai','artificial','intelligence','ml','agent','agentic','llm'], 10),

    (active_tenant(), 'Salesforce', 'salesforce', ARRAY[
        'salesforce','sfdc','apex','lightning','sales cloud','service cloud','marketing cloud',
        'experience cloud','einstein','agentforce','mulesoft','tableau','crm','data cloud',
        'slack','revenue cloud','cpq','field service lightning'],
        ARRAY['salesforce','crm','sfdc'], 20),

    (active_tenant(), 'Insurance', 'insurance', ARRAY[
        'insurance','insurtech','underwriting','claims','policy admin','policyholder',
        'actuarial','reinsurance','broker','carrier','p&c','life and annuity','annuity',
        'guidewire','duck creek','premium','loss run','first notice of loss','fnol'],
        ARRAY['insurance','insurtech','underwriting','claims','policy'], 30),

    (active_tenant(), 'BPM', 'bpm', ARRAY[
        'bpm','business process','process mining','workflow','back office','data entry',
        'data annotation','data labeling','data labelling','document processing',
        'data validation','data enrichment','shared services','bpo','outsourcing',
        'transaction processing'],
        ARRAY['bpm','process','workflow','bpo'], 40),

    (active_tenant(), 'IPA', 'ipa', ARRAY[
        'rpa','robotic process automation','intelligent automation','hyperautomation',
        'uipath','automation anywhere','blue prism','idp','intelligent document processing',
        'ocr','power automate','process automation','cognitive automation'],
        ARRAY['rpa','automation','bpa','hyperautomation'], 50),

    (active_tenant(), 'Data Engineering', 'data-engineering', ARRAY[
        'data engineering','data pipeline','etl','elt','data warehouse','data lake',
        'lakehouse','databricks','snowflake','dbt','data mesh','data fabric','data catalog',
        'data quality','data governance','business intelligence','power bi','airflow',
        'streaming','kafka','reverse etl','semantic layer'],
        ARRAY['data','tableau','powerbi','analytics','warehouse'], 60),

    (active_tenant(), 'Cloud', 'cloud', ARRAY[
        'cloud','aws','amazon web services','gcp','google cloud','kubernetes','k8s',
        'container','docker','serverless','terraform','iac','infrastructure as code',
        'finops','cloud migration','multi-cloud','hybrid cloud','cloud native','devops',
        'sre','platform engineering','observability'],
        ARRAY['cloud','azure','aws','gcp','migration','devops'], 70),

    (active_tenant(), 'Microsoft', 'microsoft', ARRAY[
        'microsoft','azure','dynamics 365','dynamics','power platform','powerapps',
        'power apps','power bi','sharepoint','microsoft 365','office 365','teams','fabric',
        'entra','copilot studio','.net','sql server'],
        ARRAY['microsoft','azure','dynamics','power'], 80),

    (active_tenant(), 'App Dev', 'app-dev', ARRAY[
        'application development','app development','software development',
        'product engineering','application modernization','application modernisation',
        'legacy modernization','microservices','api','full stack','mobile app','react',
        'node','java','application support','application maintenance','qa','testing',
        'sdlc','ci/cd'],
        ARRAY['integration','api','esb','middleware','development'], 90),

    (active_tenant(), 'AS400', 'as400', ARRAY[
        'as400','as/400','ibm i','iseries','i series','rpg','rpgle','cl program','db2',
        'power systems','midrange','green screen','cobol'],
        ARRAY['as400','iseries','ibm','rpg','legacy'], 100),

    (active_tenant(), 'Web3', 'web3', ARRAY[
        'web3','blockchain','smart contract','solidity','ethereum','defi','nft',
        'tokenization','tokenisation','dao','crypto','distributed ledger','hyperledger',
        'zero knowledge','wallet'],
        ARRAY['web3','blockchain','crypto','defi','nft'], 110),

    (active_tenant(), 'LC/NC', 'lc-nc', ARRAY[
        'low code','low-code','no code','no-code','citizen developer','mendix','outsystems',
        'appian','bubble','retool','airtable','drag and drop'],
        ARRAY['outsystems','lowcode','low-code','mendix','appian'], 120),

    (active_tenant(), 'Healthcare', 'healthcare', ARRAY[
        'healthcare','health care','hipaa','ehr','emr','epic','patient','clinical',
        'telehealth','hl7','fhir','revenue cycle','medical billing','payer',
        'provider network','interoperability'],
        ARRAY['healthcare','health','hipaa','ehr','clinical'], 130),

    (active_tenant(), 'vCTO', 'vcto', ARRAY[
        'vcto','virtual cto','fractional cto','it strategy','it roadmap',
        'technology advisory','digital strategy','it leadership','technology consulting',
        'cio advisory','it assessment'],
        ARRAY['strategy','advisory','transformation','cto'], 140),

    (active_tenant(), 'IT Staffing', 'it-staffing', ARRAY[
        'it staffing','staff augmentation','staffing','offshore team','nearshore',
        'dedicated team','hiring developers','talent','contract to hire','recruitment'],
        ARRAY['staffing','talent','recruitment','team'], 150)
ON CONFLICT (tenant_id, name) DO NOTHING;


-- Commercial intent tokens (was trend_scout.COMMERCIAL_TOKENS).
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'commercial_tokens', unnest(ARRAY[
    'services','service','solutions','solution','platform','platforms','software','tool',
    'tools','consulting','consultancy','consultant','consultants','company','companies',
    'vendor','vendors','partner','partners','provider','providers','agency','agencies',
    'firm','firms','implementation','integration','migration','modernization',
    'modernisation','transformation','automation','orchestration','optimization',
    'optimisation','governance','compliance','strategy','roadmap','framework',
    'architecture','deployment','adoption','enablement','management','monitoring',
    'analytics','engineering','development','outsourcing','support','maintenance',
    'managed','hosted','cloud-based','enterprise','pricing','cost','benefits','examples',
    'use-cases','comparison','alternatives'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- Newsroom head nouns (was trend_scout.GENERIC_HEADS).
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'generic_heads', unnest(ARRAY[
    'model','models','era','adoption','capability','capabilities','control','boom','hype',
    'race','wars','war','giant','giants','startup','startups','world','future','space',
    'landscape','ecosystem','revolution','wave','shift','push','move','moves','deal',
    'deals','funding','round','rounds','launch','launches','feature','features','update',
    'updates','version','release','releases','announcement','partnership','acquisition',
    'investment','growth','spending','budget','budgets','leader','leaders','leadership',
    'expert','experts','user','users','customer','customers','team','teams','worker',
    'workers','job','jobs','skill','skills','trend','trends','story','stories','thing',
    'things','stuff','level','levels','part','parts','piece','side','point','points',
    'area','areas','effort','efforts','plan','plans','idea','ideas','question','questions',
    'answer','answers','result','results','change','changes'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- Non-editorial outreach targets (was platform_finder.DOMAIN_BLACKLIST).
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'domain_blacklist', unnest(ARRAY[
    'g2.com','capterra.com','trustpilot.com','sitejabber.com','facebook.com','twitter.com',
    'x.com','linkedin.com','instagram.com','pinterest.com','youtube.com','reddit.com',
    'quora.com','medium.com','wordpress.com','blogspot.com','github.com','stackoverflow.com',
    'designrush.com','goodfirms.co','topdevelopers.co'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- SERP competitor classification sets (were rank_tracker module constants).
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'big_tech_domains', unnest(ARRAY[
    'cloud.google.com','aws.amazon.com','learn.microsoft.com','azure.microsoft.com',
    'openai.com','anthropic.com','ibm.com','oracle.com','sap.com','salesforce.com'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;

INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'aggregator_domains', unnest(ARRAY[
    'g2.com','capterra.com','gartner.com','forrester.com','clutch.co','goodfirms.co',
    'designrush.com','topdevelopers.co','softwareadvice.com','trustradius.com'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;

INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'informational_domains', unnest(ARRAY[
    'wikipedia.org','youtube.com','reddit.com','quora.com','medium.com'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- URL tokens that mark a revenue page (was sitemap_validator.SERVICE_KEYWORDS).
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'service_keywords', unnest(ARRAY[
    'services','solutions','consulting','development','automation','agent','integration',
    'platform','marketing','migration','modernization','maintenance','support',
    'engineering','implementation','transformation'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- URL path → page_type. `label` carries the resulting type; a NULL label means
-- "recognised, but deliberately out of scope" (WordPress taxonomy archives).
INSERT INTO tenant_vocabularies (tenant_id, kind, term, label) VALUES
-- Singular/alternate spellings are listed alongside the plurals: the Python
-- lists this replaces carried both, and achieva.ai actually publishes under
-- /success-story/ — dropping them reclassified 113 live pages.
    (active_tenant(), 'url_path_map', '/blog/',              'blog'),
    (active_tenant(), 'url_path_map', '/blogs/',             'blog'),
    (active_tenant(), 'url_path_map', '/blog-',              'blog'),
    (active_tenant(), 'url_path_map', '/insights/',          'blog'),
    (active_tenant(), 'url_path_map', '/insight/',           'blog'),
    (active_tenant(), 'url_path_map', '/articles/',          'blog'),
    (active_tenant(), 'url_path_map', '/article/',           'blog'),
    (active_tenant(), 'url_path_map', '/news/',              'blog'),
    (active_tenant(), 'url_path_map', '/posts/',             'blog'),
    (active_tenant(), 'url_path_map', '/post-',              'blog'),
    (active_tenant(), 'url_path_map', '/service_insights/',  'blog'),
    (active_tenant(), 'url_path_map', '/type_insights/',     'blog'),
    (active_tenant(), 'url_path_map', '/case-studies/',      'resource'),
    (active_tenant(), 'url_path_map', '/case-study/',        'resource'),
    (active_tenant(), 'url_path_map', '/client-success/',    'resource'),
    (active_tenant(), 'url_path_map', '/success-story/',     'resource'),
    (active_tenant(), 'url_path_map', '/success-stories/',   'resource'),
    (active_tenant(), 'url_path_map', '/whitepapers/',       'resource'),
    (active_tenant(), 'url_path_map', '/whitepaper/',        'resource'),
    (active_tenant(), 'url_path_map', '/ebooks/',            'resource'),
    (active_tenant(), 'url_path_map', '/ebook/',             'resource'),
    (active_tenant(), 'url_path_map', '/downloads/',         'resource'),
    (active_tenant(), 'url_path_map', '/webinars/',          'resource'),
    (active_tenant(), 'url_path_map', '/webinar/',           'resource'),
    (active_tenant(), 'url_path_map', '/reports/',           'resource'),
    (active_tenant(), 'url_path_map', '/report/',            'resource'),
    (active_tenant(), 'url_path_map', '/resources/',         'resource'),
    (active_tenant(), 'url_path_map', '/services_success/',  'resource'),
    (active_tenant(), 'url_path_map', '/industries_success/','resource'),
    (active_tenant(), 'url_path_map', '/industry/',          'service'),
    (active_tenant(), 'url_path_map', '/industries/',        'service'),
    (active_tenant(), 'url_path_map', '/verticals/',         'service'),
    (active_tenant(), 'url_path_map', '/glossary/',          'glossary'),
    (active_tenant(), 'url_path_map', '/lp/',                'landing'),
    (active_tenant(), 'url_path_map', '/landing/',           'landing'),
    (active_tenant(), 'url_path_map', '/campaigns/',         'landing'),
    (active_tenant(), 'url_path_map', '/category/',          NULL),
    (active_tenant(), 'url_path_map', '/tag/',               NULL),
    (active_tenant(), 'url_path_map', '/author/',            NULL),
    (active_tenant(), 'url_path_map', '/taxonomy/',          NULL)
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- Citable primary sources for guest posts (was a B2B-IT analyst list inline
-- in three places in guest_post_drafter's prompt).
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'analyst_sources', unnest(ARRAY[
    'Gartner','IBM','McKinsey','Forrester','IDC'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- House style. Unlike the definition patterns below, these regexes ARE tenant
-- policy: which words a client refuses to put in print is an editorial choice,
-- and another client would ban a different list. Stored as regex source.
--
-- Two entries are known to over-fire and are kept only because they reproduce
-- current behaviour: \brobust\b flags "robust testing" and
-- \bbest(?:-in-class)?\b flags "best practice". Retire them per client rather
-- than in code.
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'banned_claims', unnest(ARRAY[
    '\bguaranteed?\b', '\bfastest\b', '\bbest(?:-in-class)?\b',
    '#\s*1\b', '\bnumber\s+one\b',
    '100%\s+(?:success|reliable|effective|guaranteed)',
    '\bgame[-\s]?changer(?:s)?\b', '\bcutting[-\s]?edge\b',
    '\bdisrupt(?:s|ed|ing)?\b', '\bsynerg(?:y|ies)\b',
    '\bseamless(?:ly)?\b', '\brobust\b', '\bempower(?:s|ed|ing)?\b',
    '\bleverage[sd]?\b', '\bworld[-\s]?class\b',
    '\bin\s+today''?s\s+(?:fast[-\s]?paced|digital|complex|landscape|world|environment)\b'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;

INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'banned_openers', unnest(ARRAY[
    '^\s*it\s+is\s+(?:worth\s+noting|important\s+to\s+(?:note|understand))\s+that',
    '^\s*this\s+(?:means|ensures|allows|is\s+where|is\s+why)\b',
    '^\s*one\s+of\s+the\s+(?:key|most\s+important)',
    '^\s*ultimately,?\s+', '^\s*essentially,?\s+', '^\s*fundamentally,?\s+',
    '^\s*by\s+doing\s+so,?\s+', '^\s*as\s+a\s+result,?\s+',
    '^\s*let''?s\s+(?:explore|take\s+a\s+look\s+at|dive)',
    '^\s*in\s+conclusion,?\s+', '^\s*in\s+order\s+to\b',
    '^\s*for\s+the\s+purpose\s+of\b',
    '^\s*as\s+mentioned\s+(?:above|earlier|previously)\b'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- Deliberately NOT seeded here: glossary_detector.DEFINITION_PATTERNS.
-- Those are compiled regexes with named capture groups that detect English
-- definition grammar ("what is X", "X explained"). That is language logic,
-- not tenant identity — storing regex source in the database would add a
-- failure mode without making the system portable to a second client, who
-- would want the same patterns. The tenant-specific part of that module is
-- where glossary pages live, which is the `glossary_url_path` policy below.


-- Tuned numbers, each recorded with why it is what it is.
INSERT INTO tenant_policies (tenant_id, key, value, description) VALUES
    (active_tenant(), 'cwv_thresholds',
     '{"mobile": 60, "desktop": 85}'::jsonb,
     'PageSpeed pass marks. NOT Google''s 90/50 bands — these are this client''s '
     'own baseline, set when damcodigital.com scored 16 on mobile. A different '
     'site should reset them or adopt Google''s.'),

    (active_tenant(), 'inbound_link_thresholds',
     '{"pillar": 5, "service": 3}'::jsonb,
     'Minimum inbound internal links before a page is flagged under-linked. '
     'Calibrated on a 20-page property; on a large site global nav alone clears '
     'these and the detector goes permanently silent.'),

    (active_tenant(), 'thin_content_thresholds',
     '{"home": 150, "pillar": 800, "service": 300, "blog": 300, '
     '"resource": 200, "landing": 100, "glossary": 100}'::jsonb,
     'Word-count floors by page_type, tuned to a B2B services content model. '
     'A page_type absent from this map is never flagged thin.'),

    (active_tenant(), 'audit_page_types',
     '["home", "pillar", "service"]'::jsonb,
     'Default audit scope. Note the page_type classifier never emits "pillar" — '
     'that designation is currently applied by hand.'),

    (active_tenant(), 'compliance_dimension_weights',
     '{}'::jsonb,
     'Per-client weighting for compliance_checker''s 12 dimensions. Empty means '
     'use the module defaults, which must sum to 100.'),

    (active_tenant(), 'content_style',
     '{"max_em_dashes": 3, "keyword_density_band": [0.5, 2.5], '
     '"perspective": "second-person", "english": "US", '
     '"style_guide": "Chicago Manual of Style"}'::jsonb,
     'House style enforced by guest_post_drafter''s compliance scan.'),

    (active_tenant(), 'glossary_url_path',
     '"/glossary/"'::jsonb,
     'Where glossary entries live. A client using /terms/ or /wiki/ would '
     'otherwise show zero coverage and every term as a gap.')
ON CONFLICT (tenant_id, key) DO NOTHING;

COMMIT;

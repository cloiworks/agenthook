//! agenthook-tui — a ratatui config editor for agenthook's routes.json.
//!
//! Add/edit/delete webhook endpoints, write the prompt that tells the agent
//! what to do, toggle the run mode, generate HMAC secrets, and tweak global +
//! per-route agent settings — without hand-editing JSON.
//!
//! Config path: argv[1], else $AGENTHOOK_CONFIG, else ./routes.json
//!
//! List keys:  ↑/↓ move · a add · e/⏎ edit · d delete · g global · w save · q quit
//! Form keys:  ↑/↓ field · ⏎ edit · ←/→ toggle mode · ^G gen secret · ^S save · Esc cancel
//! The prompt field opens $EDITOR (nano/vi) for multi-line editing.

use std::{env, fs, io};

use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    prelude::*,
    widgets::{Block, Borders, Cell, Clear, List, ListItem, ListState, Paragraph, Row, Table, Wrap},
};
use serde_json::{Map, Value};

// ---------------------------------------------------------------------------
// Config data layer (no UI)
// ---------------------------------------------------------------------------

fn config_path() -> String {
    if let Some(a) = env::args().nth(1) {
        return a;
    }
    env::var("AGENTHOOK_CONFIG").unwrap_or_else(|_| "routes.json".to_string())
}

fn home() -> String {
    env::var("HOME").unwrap_or_else(|_| ".".to_string())
}

fn load_config(path: &str) -> Value {
    let mut cfg: Value = fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| Value::Object(Map::new()));
    if !cfg.is_object() {
        cfg = Value::Object(Map::new());
    }
    let obj = cfg.as_object_mut().unwrap();
    obj.entry("host").or_insert(Value::String("127.0.0.1".into()));
    obj.entry("port").or_insert(Value::from(8644));
    let agent = obj
        .entry("agent")
        .or_insert_with(|| Value::Object(Map::new()));
    if let Some(a) = agent.as_object_mut() {
        a.entry("bin")
            .or_insert(Value::String(format!("{}/.local/bin/claude", home())));
        a.entry("workdir").or_insert(Value::String(home()));
        a.entry("timeout").or_insert(Value::from(1800));
    }
    obj.entry("routes").or_insert_with(|| Value::Object(Map::new()));
    cfg
}

fn save_config(path: &str, cfg: &Value) -> io::Result<()> {
    let tmp = format!("{path}.tmp");
    let mut s = serde_json::to_string_pretty(cfg).unwrap_or_else(|_| "{}".into());
    s.push('\n');
    fs::write(&tmp, s)?;
    fs::rename(&tmp, path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// 16 random bytes from the OS, hex-encoded -> a 32-char HMAC secret.
fn gen_secret() -> String {
    // /dev/urandom is an infinite stream — read exactly 16 bytes, never to EOF.
    let bytes = {
        use std::io::Read;
        fs::File::open("/dev/urandom").ok().and_then(|mut f| {
            let mut b = [0u8; 16];
            f.read_exact(&mut b).ok().map(|_| b.to_vec())
        })
    };
    let buf: Vec<u8> = match bytes {
        Some(b) if b.len() >= 16 => b[..16].to_vec(),
        _ => {
            // fallback: time-seeded, weak but non-fatal
            let t = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0);
            t.to_le_bytes().iter().cycle().take(16).copied().collect()
        }
    };
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

fn routes_obj(cfg: &Value) -> &Map<String, Value> {
    cfg.get("routes").and_then(|r| r.as_object()).unwrap()
}

fn val_to_string(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        Some(Value::Array(a)) => a
            .iter()
            .map(|x| match x {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .collect::<Vec<_>>()
            .join(", "),
        _ => String::new(),
    }
}

fn csv_to_array(s: &str) -> Value {
    Value::Array(
        s.split(',')
            .map(|x| x.trim())
            .filter(|x| !x.is_empty())
            .map(|x| Value::String(x.to_string()))
            .collect(),
    )
}

// ---------------------------------------------------------------------------
// Field model
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    Name,
    Mode,
    Text,
    Secret,
    Csv,
    Prompt,
    AgentText,
    AgentCsv,
}

struct Field {
    label: &'static str,
    key: &'static str, // for AgentText/AgentCsv this is the agent sub-key
    kind: Kind,
}

fn route_fields() -> Vec<Field> {
    vec![
        Field { label: "엔드포인트 이름", key: "__name__", kind: Kind::Name },
        Field { label: "실행 모드", key: "mode", kind: Kind::Mode },
        Field { label: "시크릿 (HMAC)", key: "secret", kind: Kind::Secret },
        Field { label: "이벤트 필터", key: "events", kind: Kind::Csv },
        Field { label: "프롬프트 (작업 내용)", key: "prompt", kind: Kind::Prompt },
        Field { label: "agent.model", key: "model", kind: Kind::AgentText },
        Field { label: "agent.add_dir", key: "add_dir", kind: Kind::AgentCsv },
        Field { label: "agent.timeout(초)", key: "timeout", kind: Kind::AgentText },
    ]
}

fn global_fields() -> Vec<Field> {
    vec![
        Field { label: "바인드 호스트", key: "host", kind: Kind::Text },
        Field { label: "포트", key: "port", kind: Kind::Text },
        Field { label: "전역 fallback secret", key: "secret", kind: Kind::Secret },
        Field { label: "agent.bin (claude 경로)", key: "bin", kind: Kind::AgentText },
        Field { label: "agent.workdir (실행 cwd)", key: "workdir", kind: Kind::AgentText },
        Field { label: "agent.timeout 초", key: "timeout", kind: Kind::AgentText },
        Field { label: "agent.model (선택)", key: "model", kind: Kind::AgentText },
    ]
}

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------

#[derive(PartialEq)]
enum Screen {
    List,
    Route,
    Global,
}

struct App {
    path: String,
    cfg: Value,
    dirty: bool,
    screen: Screen,
    list: ListState,
    // form state
    fields: Vec<Field>,
    field_idx: usize,
    form_name: String,    // route name being edited (Route screen)
    orig_name: String,    // "" when adding
    form_route: Value,    // working copy of the route object (Route screen)
    editing: Option<String>, // Some(buffer) when in line-input mode
    status: String,
    confirm_delete: bool,
}

impl App {
    fn new(path: String) -> Self {
        let cfg = load_config(&path);
        let mut list = ListState::default();
        if !routes_obj(&cfg).is_empty() {
            list.select(Some(0));
        }
        App {
            path,
            cfg,
            dirty: false,
            screen: Screen::List,
            list,
            fields: Vec::new(),
            field_idx: 0,
            form_name: String::new(),
            orig_name: String::new(),
            form_route: Value::Object(Map::new()),
            editing: None,
            status: String::new(),
            confirm_delete: false,
        }
    }

    fn route_names(&self) -> Vec<String> {
        routes_obj(&self.cfg).keys().cloned().collect()
    }

    fn selected_name(&self) -> Option<String> {
        let names = self.route_names();
        self.list.selected().and_then(|i| names.get(i).cloned())
    }

    // ---- form open/commit ----

    fn open_route_form(&mut self, name: &str) {
        self.orig_name = name.to_string();
        self.form_name = name.to_string();
        self.form_route = if name.is_empty() {
            let mut m = Map::new();
            m.insert("secret".into(), Value::String(gen_secret()));
            m.insert("mode".into(), Value::String("agent".into()));
            m.insert("prompt".into(), Value::String(String::new()));
            Value::Object(m)
        } else {
            routes_obj(&self.cfg).get(name).cloned().unwrap_or(Value::Object(Map::new()))
        };
        self.fields = route_fields();
        self.field_idx = 0;
        self.editing = None;
        self.screen = Screen::Route;
    }

    fn open_global_form(&mut self) {
        self.fields = global_fields();
        self.field_idx = 0;
        self.editing = None;
        self.screen = Screen::Global;
    }

    fn validate_route(&self) -> Option<String> {
        let name = self.form_name.trim();
        if name.is_empty() {
            return Some("엔드포인트 이름이 비었습니다.".into());
        }
        if name.contains(' ') || name.contains('/') {
            return Some("이름에 공백/슬래시는 쓸 수 없습니다.".into());
        }
        if name != self.orig_name && routes_obj(&self.cfg).contains_key(name) {
            return Some(format!("'{name}' 라우트가 이미 있습니다."));
        }
        let mode = self.form_route.get("mode").and_then(|v| v.as_str()).unwrap_or("");
        if mode != "agent" && mode != "log" {
            return Some("mode 는 agent 또는 log 여야 합니다.".into());
        }
        if val_to_string(self.form_route.get("secret")).trim().is_empty() {
            return Some("secret 이 비었습니다 (테스트는 INSECURE_NO_AUTH).".into());
        }
        if val_to_string(self.form_route.get("prompt")).trim().is_empty() {
            return Some("prompt 가 비었습니다.".into());
        }
        None
    }

    fn commit_route(&mut self) -> bool {
        if let Some(err) = self.validate_route() {
            self.status = format!("✗ {err}");
            return false;
        }
        // prune empty optional fields
        if let Some(obj) = self.form_route.as_object_mut() {
            if obj.get("events").map(|v| val_to_string(Some(v)).is_empty()).unwrap_or(false) {
                obj.shift_remove("events");
            }
            if let Some(Value::Object(a)) = obj.get_mut("agent") {
                let empties: Vec<String> = a
                    .iter()
                    .filter(|(_, v)| val_to_string(Some(v)).is_empty())
                    .map(|(k, _)| k.clone())
                    .collect();
                for k in empties {
                    a.shift_remove(&k);
                }
                if a.is_empty() {
                    obj.shift_remove("agent");
                }
            }
        }
        let routes = self.cfg.get_mut("routes").unwrap().as_object_mut().unwrap();
        if !self.orig_name.is_empty() && self.orig_name != self.form_name {
            routes.shift_remove(&self.orig_name);
        }
        routes.insert(self.form_name.clone(), self.form_route.clone());
        self.dirty = true;
        self.screen = Screen::List;
        // keep selection on the edited route
        let names = self.route_names();
        if let Some(i) = names.iter().position(|n| n == &self.form_name) {
            self.list.select(Some(i));
        }
        true
    }

    fn save(&mut self) {
        match save_config(&self.path, &self.cfg) {
            Ok(()) => {
                self.dirty = false;
                self.status = format!("✓ 저장됨: {} (chmod 600)", self.path);
            }
            Err(e) => self.status = format!("✗ 저장 실패: {e}"),
        }
    }

    // ---- field value get/set within the active form ----

    fn agent_map_mut<'a>(route: &'a mut Value) -> &'a mut Map<String, Value> {
        let obj = route.as_object_mut().unwrap();
        obj.entry("agent")
            .or_insert_with(|| Value::Object(Map::new()))
            .as_object_mut()
            .unwrap()
    }

    fn field_display(&self, f: &Field) -> String {
        match (&self.screen, f.kind) {
            (_, Kind::Name) => self.form_name.clone(),
            (_, Kind::Mode) => self
                .form_route
                .get("mode")
                .and_then(|v| v.as_str())
                .unwrap_or("agent")
                .to_string(),
            (Screen::Route, Kind::Prompt) => val_to_string(self.form_route.get("prompt"))
                .replace('\n', "⏎"),
            (Screen::Route, Kind::AgentText | Kind::AgentCsv) => {
                let a = self.form_route.get("agent");
                val_to_string(a.and_then(|a| a.get(f.key)))
            }
            (Screen::Route, _) => val_to_string(self.form_route.get(f.key)),
            (Screen::Global, Kind::AgentText | Kind::AgentCsv) => {
                let a = self.cfg.get("agent");
                val_to_string(a.and_then(|a| a.get(f.key)))
            }
            (Screen::Global, _) => val_to_string(self.cfg.get(f.key)),
            _ => String::new(),
        }
    }

    fn current_field_initial(&self) -> String {
        let f = &self.fields[self.field_idx];
        match f.kind {
            Kind::Name => self.form_name.clone(),
            Kind::AgentText | Kind::AgentCsv => {
                let src = if self.screen == Screen::Global { &self.cfg } else { &self.form_route };
                val_to_string(src.get("agent").and_then(|a| a.get(f.key)))
            }
            _ => {
                let src = if self.screen == Screen::Global { &self.cfg } else { &self.form_route };
                val_to_string(src.get(f.key))
            }
        }
    }

    fn apply_input(&mut self, buf: String) {
        let f_kind = self.fields[self.field_idx].kind;
        let f_key = self.fields[self.field_idx].key.to_string();
        let global = self.screen == Screen::Global;
        match f_kind {
            Kind::Name => self.form_name = buf.trim().to_string(),
            Kind::Csv => {
                self.form_route.as_object_mut().unwrap().insert(f_key, csv_to_array(&buf));
            }
            Kind::AgentCsv => {
                let target = if global { &mut self.cfg } else { &mut self.form_route };
                Self::agent_map_mut(target).insert(f_key, csv_to_array(&buf));
            }
            Kind::AgentText => {
                let target = if global { &mut self.cfg } else { &mut self.form_route };
                let v = if f_key == "timeout" {
                    buf.trim().parse::<i64>().map(Value::from).unwrap_or(Value::String(buf.trim().into()))
                } else {
                    Value::String(buf.trim().into())
                };
                Self::agent_map_mut(target).insert(f_key, v);
            }
            Kind::Text | Kind::Secret => {
                let target = if global { &mut self.cfg } else { &mut self.form_route };
                let v = if f_key == "port" {
                    buf.trim().parse::<i64>().map(Value::from).unwrap_or(Value::String(buf.trim().into()))
                } else {
                    Value::String(buf)
                };
                target.as_object_mut().unwrap().insert(f_key, v);
            }
            Kind::Prompt => {
                self.form_route
                    .as_object_mut()
                    .unwrap()
                    .insert("prompt".into(), Value::String(buf));
            }
            Kind::Mode => {}
        }
    }

    fn toggle_mode(&mut self) {
        let cur = self
            .form_route
            .get("mode")
            .and_then(|v| v.as_str())
            .unwrap_or("agent");
        let next = if cur == "agent" { "log" } else { "agent" };
        self.form_route
            .as_object_mut()
            .unwrap()
            .insert("mode".into(), Value::String(next.into()));
    }

    fn gen_secret_field(&mut self) {
        let global = self.screen == Screen::Global;
        let target = if global { &mut self.cfg } else { &mut self.form_route };
        target
            .as_object_mut()
            .unwrap()
            .insert("secret".into(), Value::String(gen_secret()));
    }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

fn draw(f: &mut Frame, app: &mut App) {
    match app.screen {
        Screen::List => draw_list(f, app),
        Screen::Route | Screen::Global => draw_form(f, app),
    }
}

fn draw_list(f: &mut Frame, app: &mut App) {
    let area = f.area();
    let chunks = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(2),
    ])
    .split(area);

    let host = val_to_string(app.cfg.get("host"));
    let port = val_to_string(app.cfg.get("port"));
    let title = format!(
        " agenthook 설정  ·  {host}:{port}  ·  {}{} ",
        app.path,
        if app.dirty { "  ● 저장 안 됨" } else { "" }
    );
    f.render_widget(
        Paragraph::new(title).style(Style::new().bold().bg(if app.dirty {
            Color::Rgb(80, 40, 40)
        } else {
            Color::Reset
        })),
        chunks[0],
    );

    let names = app.route_names();
    let items: Vec<ListItem> = if names.is_empty() {
        vec![ListItem::new("  라우트가 없습니다.  'a' 로 엔드포인트를 추가하세요.")
            .style(Style::new().dim())]
    } else {
        names
            .iter()
            .map(|nm| {
                let r = &routes_obj(&app.cfg)[nm];
                let mode = r.get("mode").and_then(|v| v.as_str()).unwrap_or("agent");
                let open = r.get("secret").and_then(|v| v.as_str()) == Some("INSECURE_NO_AUTH");
                let sec = if open { "open" } else { "🔒" };
                let prev = val_to_string(r.get("prompt")).replace('\n', " ");
                let prev: String = prev.chars().take(48).collect();
                ListItem::new(format!("/webhooks/{nm:<14} [{mode}] {sec}   {prev}"))
            })
            .collect()
    };
    let list = List::new(items)
        .block(Block::default().borders(Borders::TOP).title(" 엔드포인트 "))
        .highlight_style(Style::new().reversed().bold())
        .highlight_symbol("▶ ");
    f.render_stateful_widget(list, chunks[1], &mut app.list);

    let help = if app.confirm_delete {
        Line::from(vec![Span::styled(
            format!("'{}' 삭제? (y/N)", app.selected_name().unwrap_or_default()),
            Style::new().bold().fg(Color::Red),
        )])
    } else if !app.status.is_empty() {
        Line::from(Span::styled(app.status.clone(), Style::new().bold()))
    } else {
        Line::from(Span::styled(
            "↑/↓ 선택 · a 추가 · e/⏎ 편집 · d 삭제 · g 전역설정 · w 저장 · q 종료",
            Style::new().dim(),
        ))
    };
    f.render_widget(Paragraph::new(help), chunks[2]);
}

/// Char-aware truncation with an ellipsis (keeps Korean/multibyte intact).
fn clip(s: &str, max: usize) -> String {
    if s.chars().count() > max {
        s.chars().take(max).collect::<String>() + "…"
    } else {
        s.to_string()
    }
}

fn draw_form(f: &mut Frame, app: &mut App) {
    let area = f.area();
    let chunks = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(2),
    ])
    .split(area);

    let title = match app.screen {
        Screen::Global => " 전역 설정 ".to_string(),
        _ => format!(
            " 라우트 편집: {} ",
            if app.form_name.is_empty() { "(새 엔드포인트)" } else { &app.form_name }
        ),
    };
    f.render_widget(
        Paragraph::new(title).style(Style::new().bold().fg(Color::Yellow)),
        chunks[0],
    );

    let rows: Vec<Row> = app
        .fields
        .iter()
        .enumerate()
        .map(|(i, fld)| {
            let selected = i == app.field_idx;
            let value = if fld.kind == Kind::Mode {
                let cur = app.form_route.get("mode").and_then(|v| v.as_str()).unwrap_or("agent");
                let a = if cur == "agent" { "[ agent ]" } else { "  agent  " };
                let l = if cur == "log" { "[ log ]" } else { "  log  " };
                format!("{a} {l}")
            } else {
                clip(&app.field_display(fld), 200)
            };
            let marker = if selected { "▶ " } else { "  " };
            let val_cell = if selected {
                Cell::from(value)
            } else {
                Cell::from(value).style(Style::new().fg(Color::Cyan))
            };
            let row = Row::new(vec![Cell::from(format!("{marker}{}", fld.label)), val_cell]);
            if selected { row.style(Style::new().reversed().bold()) } else { row }
        })
        .collect();
    let table = Table::new(rows, [Constraint::Length(30), Constraint::Min(10)])
        .column_spacing(2)
        .block(Block::default().borders(Borders::ALL));
    f.render_widget(table, chunks[1]);

    // bottom: status or contextual help
    let bottom = if !app.status.is_empty() {
        Paragraph::new(Line::from(Span::styled(app.status.clone(), Style::new().bold())))
    } else {
        let hint = match app.fields[app.field_idx].kind {
            Kind::Name => "POST /webhooks/<이름> 로 호출됨 · ⏎ 편집 · ^S 저장 · Esc 취소",
            Kind::Mode => "←/→/Space 모드 토글  (agent=에이전트 실행 / log=드라이런)",
            Kind::Prompt => "⏎ $EDITOR 로 프롬프트 작성 · ^S 저장 · Esc 취소",
            Kind::Secret => "⏎ 편집 · ^G 시크릿 생성 · ^S 저장 · Esc 취소",
            _ => "⏎ 편집 · ^S 저장 · Esc 취소",
        };
        Paragraph::new(Line::from(Span::styled(hint, Style::new().dim())))
    };
    f.render_widget(bottom, chunks[2]);

    // editing input rendered as a centered popup so it's unmissable
    if let Some(buf) = &app.editing {
        let label = app.fields[app.field_idx].label;
        let multiline = app.fields[app.field_idx].kind == Kind::Prompt;
        let (w, h, title, shown) = if multiline {
            let w = ((area.width as u32 * 8 / 10) as u16).clamp(40, 100);
            let h = area.height.saturating_sub(4).clamp(6, 18);
            (w, h, format!(" {label}  (⏎ 줄바꿈 · ^S 저장 · Esc 취소) "), format!("{buf}█"))
        } else {
            let w = area.width.saturating_sub(8).min(84).max(24);
            (w, 3u16, format!(" 편집: {label}  (⏎ 확정 · Esc 취소) "), format!("{}█", clip(buf, 400)))
        };
        let x = area.x + area.width.saturating_sub(w) / 2;
        let y = area.y + area.height.saturating_sub(h) / 2;
        let popup = Rect { x, y, width: w, height: h };
        f.render_widget(Clear, popup);
        f.render_widget(
            Paragraph::new(shown)
                .wrap(Wrap { trim: false })
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::new().fg(Color::Yellow))
                        .title(title),
                )
                .style(Style::new().fg(Color::White).bg(Color::Rgb(25, 25, 45))),
            popup,
        );
    }
}

// ---------------------------------------------------------------------------
// Event handling
// ---------------------------------------------------------------------------

enum Action {
    None,
    Quit,
}

fn on_key(app: &mut App, key: KeyEvent) -> Action {
    // line-input mode captures everything first
    if app.editing.is_some() {
        let multiline = app
            .fields
            .get(app.field_idx)
            .map(|f| f.kind == Kind::Prompt)
            .unwrap_or(false);
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Esc => app.editing = None,
            // ^S confirms (the only way to finish a multiline prompt)
            KeyCode::Char('s') if ctrl => {
                let b = app.editing.take().unwrap();
                app.apply_input(b);
            }
            KeyCode::Enter | KeyCode::Char('\n') | KeyCode::Char('\r') => {
                if multiline {
                    app.editing.as_mut().unwrap().push('\n');
                } else {
                    let b = app.editing.take().unwrap();
                    app.apply_input(b);
                }
            }
            KeyCode::Backspace => {
                app.editing.as_mut().unwrap().pop();
            }
            KeyCode::Char(c) => app.editing.as_mut().unwrap().push(c),
            _ => {}
        }
        return Action::None;
    }

    app.status.clear();

    match app.screen {
        Screen::List => on_key_list(app, key),
        Screen::Route => on_key_route(app, key),
        Screen::Global => on_key_global(app, key),
    }
}

fn on_key_list(app: &mut App, key: KeyEvent) -> Action {
    if app.confirm_delete {
        if let KeyCode::Char('y') | KeyCode::Char('Y') = key.code {
            if let Some(nm) = app.selected_name() {
                app.cfg.get_mut("routes").unwrap().as_object_mut().unwrap().shift_remove(&nm);
                app.dirty = true;
                let len = app.route_names().len();
                if len == 0 {
                    app.list.select(None);
                } else {
                    let i = app.list.selected().unwrap_or(0).min(len - 1);
                    app.list.select(Some(i));
                }
            }
        }
        app.confirm_delete = false;
        return Action::None;
    }

    let len = app.route_names().len();
    match key.code {
        KeyCode::Char('q') | KeyCode::Char('Q') => {
            if app.dirty {
                app.status = "저장 안 됨 — w 로 저장하거나 다시 q 두 번? (지금은 q로 종료됨)".into();
            }
            return Action::Quit;
        }
        KeyCode::Char('w') | KeyCode::Char('W') => app.save(),
        KeyCode::Char('g') | KeyCode::Char('G') => app.open_global_form(),
        KeyCode::Char('a') | KeyCode::Char('A') => app.open_route_form(""),
        KeyCode::Char('d') | KeyCode::Char('D') => {
            if app.selected_name().is_some() {
                app.confirm_delete = true;
            }
        }
        KeyCode::Char('e') | KeyCode::Char('E') | KeyCode::Enter
        | KeyCode::Char('\n') | KeyCode::Char('\r') => {
            if let Some(nm) = app.selected_name() {
                app.open_route_form(&nm);
            }
        }
        KeyCode::Down | KeyCode::Char('j') => {
            if len > 0 {
                let i = app.list.selected().map(|i| (i + 1) % len).unwrap_or(0);
                app.list.select(Some(i));
            }
        }
        KeyCode::Up | KeyCode::Char('k') => {
            if len > 0 {
                let i = app.list.selected().map(|i| (i + len - 1) % len).unwrap_or(0);
                app.list.select(Some(i));
            }
        }
        _ => {}
    }
    Action::None
}

fn form_nav(app: &mut App, key: KeyEvent) -> bool {
    let n = app.fields.len();
    match key.code {
        KeyCode::Down | KeyCode::Tab => {
            app.field_idx = (app.field_idx + 1) % n;
            true
        }
        KeyCode::Up | KeyCode::BackTab => {
            app.field_idx = (app.field_idx + n - 1) % n;
            true
        }
        _ => false,
    }
}

fn on_key_route(app: &mut App, key: KeyEvent) -> Action {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    if ctrl && matches!(key.code, KeyCode::Char('s')) {
        app.commit_route();
        return Action::None;
    }
    if ctrl && matches!(key.code, KeyCode::Char('g')) {
        if app.fields[app.field_idx].kind == Kind::Secret {
            app.gen_secret_field();
        }
        return Action::None;
    }
    if let KeyCode::Esc = key.code {
        app.screen = Screen::List;
        return Action::None;
    }
    if form_nav(app, key) {
        return Action::None;
    }
    let kind = app.fields[app.field_idx].kind;
    match key.code {
        KeyCode::Left | KeyCode::Right | KeyCode::Char(' ') if kind == Kind::Mode => {
            app.toggle_mode();
        }
        KeyCode::Enter | KeyCode::Char('\n') | KeyCode::Char('\r') => match kind {
            Kind::Mode => app.toggle_mode(),
            _ => app.editing = Some(app.current_field_initial()),
        },
        _ => {}
    }
    Action::None
}

fn on_key_global(app: &mut App, key: KeyEvent) -> Action {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    if ctrl && matches!(key.code, KeyCode::Char('g')) {
        if app.fields[app.field_idx].kind == Kind::Secret {
            app.gen_secret_field();
            app.dirty = true;
        }
        return Action::None;
    }
    if matches!(key.code, KeyCode::Esc) || (ctrl && matches!(key.code, KeyCode::Char('s'))) {
        app.screen = Screen::List;
        return Action::None;
    }
    if form_nav(app, key) {
        return Action::None;
    }
    if matches!(key.code, KeyCode::Enter | KeyCode::Char('\n') | KeyCode::Char('\r')) {
        app.editing = Some(app.current_field_initial());
        app.dirty = true;
    }
    Action::None
}

// ---------------------------------------------------------------------------
// main loop
// ---------------------------------------------------------------------------

fn main() -> io::Result<()> {
    let path = config_path();
    let mut app = App::new(path);
    let mut terminal = ratatui::init();

    let res = run(&mut terminal, &mut app);

    ratatui::restore();
    res
}

fn run(terminal: &mut ratatui::DefaultTerminal, app: &mut App) -> io::Result<()> {
    loop {
        terminal.draw(|f| draw(f, app))?;

        let Event::Key(key) = event::read()? else { continue };
        if key.kind != KeyEventKind::Press {
            continue;
        }

        match on_key(app, key) {
            Action::Quit => return Ok(()),
            Action::None => {}
        }
    }
}

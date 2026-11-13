package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

//go:embed web/*
var assets embed.FS

type pendingSubmission struct {
	ServerURL string          `json:"server_url"`
	Payload   json.RawMessage `json:"payload"`
}

type application struct {
	mu       sync.RWMutex
	action   sync.Mutex
	dir      string
	host     string
	token    string
	url      string
	code     string
	catalog  json.RawMessage
	pending  *pendingSubmission
	remote   *http.Client
	shutdown func()
}

func newApplication(dir string) (*application, error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	random := make([]byte, 32)
	if _, err := rand.Read(random); err != nil {
		return nil, err
	}
	app := &application{dir: dir, token: hex.EncodeToString(random), remote: &http.Client{
		Timeout:       30 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}}
	var settings struct {
		ServerURL string `json:"server_url"`
	}
	if raw, err := os.ReadFile(filepath.Join(dir, "settings.json")); err == nil {
		if err := json.Unmarshal(raw, &settings); err != nil {
			return nil, fmt.Errorf("无法读取客户端连接设置: %w", err)
		}
		app.url = settings.ServerURL
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	if raw, err := os.ReadFile(filepath.Join(dir, "pending.json")); err == nil {
		var pending pendingSubmission
		if err := json.Unmarshal(raw, &pending); err != nil || pending.ServerURL == "" || !json.Valid(pending.Payload) {
			return nil, errors.New("无法读取上次提交记录. 请保留 pending.json, 联系管理员核对后再录分")
		}
		var payload map[string]any
		if json.Unmarshal(pending.Payload, &payload) != nil || payload == nil {
			return nil, errors.New("上次提交记录格式不正确, 请联系管理员核对 pending.json")
		}
		// MarshalIndent also formats embedded JSON. Normalize it before comparing a retry.
		pending.Payload, _ = json.Marshal(payload)
		app.pending = &pending
		app.url = pending.ServerURL
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	return app, nil
}

func atomicJSON(path string, value any) error {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".blue3-*.tmp")
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	if _, err = file.Write(raw); err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(name, path)
}

func normalizedURL(value string) (string, error) {
	value = strings.TrimSpace(value)
	if !strings.Contains(value, "://") {
		value = "http://" + value
	}
	parsed, err := url.Parse(value)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") ||
		parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("请填写有效的服务器地址, 例如 http://192.168.1.100:8766")
	}
	return strings.TrimRight(parsed.String(), "/"), nil
}

func (app *application) request(ctx context.Context, serverURL, code, method, path string, payload []byte) (int, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, serverURL+path, bytes.NewReader(payload))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Authorization", "Bearer "+code)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	response, err := app.remote.Do(req)
	if err != nil {
		return 0, nil, errors.New("连接中断或服务器未响应. 请检查服务器地址、网络及服务器是否正在运行")
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 4*1024*1024+1))
	if err != nil || len(raw) > 4*1024*1024 || !json.Valid(raw) {
		return 0, nil, errors.New("未收到完整的录分服务器响应, 请检查地址或重试")
	}
	return response.StatusCode, raw, nil
}

func sendJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func failure(w http.ResponseWriter, status int, message string, pending bool) {
	sendJSON(w, status, map[string]any{"error": message, "pending": pending})
}

func decode(w http.ResponseWriter, r *http.Request, value any) error {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128*1024))
	if err := decoder.Decode(value); err != nil {
		return errors.New("提交内容格式不正确或过大")
	}
	if decoder.Decode(new(any)) != io.EOF {
		return errors.New("提交内容格式不正确")
	}
	return nil
}

func (app *application) config(w http.ResponseWriter) {
	app.mu.RLock()
	defer app.mu.RUnlock()
	sendJSON(w, 200, map[string]any{"token": app.token, "server_url": app.url,
		"connected": app.code != "" && len(app.catalog) > 0, "catalog": app.catalog,
		"pending": app.pending, "version": clientVersion})
}

func (app *application) connect(w http.ResponseWriter, r *http.Request) {
	var input struct {
		ServerURL string `json:"server_url"`
		Code      string `json:"access_code"`
	}
	if err := decode(w, r, &input); err != nil {
		failure(w, 400, err.Error(), false)
		return
	}
	serverURL, err := normalizedURL(input.ServerURL)
	if err != nil {
		failure(w, 400, err.Error(), false)
		return
	}
	app.action.Lock()
	defer app.action.Unlock()
	app.mu.RLock()
	pending := app.pending != nil
	wrongTarget := pending && app.pending.ServerURL != serverURL
	if input.Code == "" && serverURL == app.url {
		input.Code = app.code
	}
	app.mu.RUnlock()
	if wrongTarget {
		failure(w, 409, "请先连接原服务器并重试上次提交, 确认结果后才能更换服务器", true)
		return
	}
	input.Code = strings.TrimSpace(input.Code)
	if input.Code == "" || len(input.Code) > 100 {
		failure(w, 400, "请输入服务器管理员提供的录分码", pending)
		return
	}
	status, raw, err := app.request(r.Context(), serverURL, input.Code, "GET", "/api/entry/options", nil)
	if err != nil {
		failure(w, 502, err.Error(), pending)
		return
	}
	if status != 200 {
		var message struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(raw, &message)
		if message.Error == "" {
			message.Error = "服务器拒绝连接, 请确认使用录分端口 8766"
		}
		failure(w, status, message.Error, pending)
		return
	}
	var catalog struct {
		OK           bool              `json:"ok"`
		APIVersion   int               `json:"api_version"`
		ServerID     string            `json:"server_id"`
		Players      []json.RawMessage `json:"players"`
		Competitions []json.RawMessage `json:"competitions"`
	}
	if json.Unmarshal(raw, &catalog) != nil || !catalog.OK || catalog.APIVersion != 1 || catalog.ServerID == "" ||
		catalog.Players == nil || catalog.Competitions == nil {
		failure(w, 502, "此地址不是兼容的 Blue3 录分服务器", pending)
		return
	}
	if err = atomicJSON(filepath.Join(app.dir, "settings.json"), map[string]string{"server_url": serverURL}); err != nil {
		failure(w, 500, "无法保存客户端设置: "+err.Error(), pending)
		return
	}
	app.mu.Lock()
	app.url, app.code, app.catalog = serverURL, input.Code, append(json.RawMessage(nil), raw...)
	app.mu.Unlock()
	sendJSON(w, 200, json.RawMessage(raw))
}

func (app *application) clearPending() error {
	if err := os.Remove(filepath.Join(app.dir, "pending.json")); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	app.mu.Lock()
	app.pending = nil
	app.mu.Unlock()
	return nil
}

const clientVersion = "1.3.0"

var requestKeyPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{8,80}$`)

func (app *application) submit(w http.ResponseWriter, r *http.Request) {
	var input map[string]any
	if err := decode(w, r, &input); err != nil || input == nil {
		failure(w, 400, "录分内容格式不正确", false)
		return
	}
	key, _ := input["request_key"].(string)
	if !requestKeyPattern.MatchString(key) {
		failure(w, 400, "提交标识不正确, 请重新打开录分页面", false)
		return
	}
	app.action.Lock()
	defer app.action.Unlock()
	app.mu.RLock()
	serverURL, code, pending := app.url, app.code, app.pending
	app.mu.RUnlock()
	if code == "" {
		failure(w, 401, "请先连接录分服务器", pending != nil)
		return
	}
	// New rounds always use rate 1. Preserve an already pending payload exactly
	// so retrying an older client submission cannot change its historical result.
	if pending == nil {
		input["rate"] = "1"
	}
	raw, _ := json.Marshal(input)
	if pending != nil && (pending.ServerURL != serverURL || !bytes.Equal(pending.Payload, raw)) {
		failure(w, 409, "上次提交结果尚未确认, 请重试原来的提交", true)
		return
	}
	if pending == nil {
		pending = &pendingSubmission{ServerURL: serverURL, Payload: raw}
		if err := atomicJSON(filepath.Join(app.dir, "pending.json"), pending); err != nil {
			failure(w, 500, "无法保存提交记录, 尚未发送成绩: "+err.Error(), false)
			return
		}
		app.mu.Lock()
		app.pending = pending
		app.mu.Unlock()
	}
	status, reply, err := app.request(r.Context(), serverURL, code, "POST", "/api/entry/matches", raw)
	if err != nil {
		failure(w, 502, err.Error()+". 本次内容已保留, 重试不会重复录入", true)
		return
	}
	var result struct {
		OK    bool   `json:"ok"`
		ID    int64  `json:"id"`
		Error string `json:"error"`
	}
	if json.Unmarshal(reply, &result) != nil {
		failure(w, 502, "提交结果尚未确认, 请重试本次提交", true)
		return
	}
	if (status == 200 || status == 201) && result.OK && result.ID > 0 {
		if err := app.clearPending(); err != nil {
			failure(w, 500, "服务器已收到成绩, 本地确认记录未更新. 请重试以确认原记录", true)
			return
		}
		sendJSON(w, status, json.RawMessage(reply))
		return
	}
	uncertain := status < 400 || status >= 500 || status == 408
	if !uncertain {
		if err := app.clearPending(); err != nil {
			uncertain = true
		}
	}
	if result.Error == "" {
		result.Error = "服务器暂时无法确认提交结果, 请重试"
	}
	if status < 400 || status > 599 {
		status = 502
	}
	failure(w, status, result.Error, uncertain)
}

func (app *application) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
	if app.host != "" && r.Host != app.host {
		failure(w, 403, "客户端地址不正确", false)
		return
	}
	if r.Method == "POST" {
		token := r.Header.Get("X-Client-Token")
		origin := r.Header.Get("Origin")
		if subtle.ConstantTimeCompare([]byte(token), []byte(app.token)) != 1 ||
			(origin != "" && origin != "http://"+r.Host) {
			failure(w, 403, "客户端连接已更新, 请刷新页面", false)
			return
		}
		switch r.URL.Path {
		case "/api/connect":
			app.connect(w, r)
		case "/api/submit":
			app.submit(w, r)
		case "/api/quit":
			sendJSON(w, 200, map[string]bool{"ok": true})
			if app.shutdown != nil {
				go app.shutdown()
			}
		default:
			failure(w, 404, "页面不存在", false)
		}
		return
	}
	if r.Method != "GET" {
		failure(w, 405, "不支持此操作", false)
		return
	}
	switch r.URL.Path {
	case "/api/health":
		sendJSON(w, 200, map[string]string{"service": "blue3-score-entry-client", "version": clientVersion})
	case "/api/config":
		app.config(w)
	case "/", "/index.html", "/app.js", "/form.js", "/app.css", "/favicon.svg", "/third-party.txt":
		name := strings.TrimPrefix(r.URL.Path, "/")
		if name == "" {
			name = "index.html"
		}
		contentType := map[string]string{"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "form.js": "text/javascript; charset=utf-8",
			"app.css": "text/css; charset=utf-8", "favicon.svg": "image/svg+xml", "third-party.txt": "text/plain; charset=utf-8"}[name]
		body, err := assets.ReadFile("web/" + name)
		if err != nil {
			failure(w, 500, "客户端文件缺失", false)
			return
		}
		w.Header().Set("Content-Type", contentType)
		_, _ = w.Write(body)
	default:
		failure(w, 404, "页面不存在", false)
	}
}

func run() error {
	noBrowser := flag.Bool("no-browser", false, "Do not open a browser automatically")
	listen := flag.String("listen", "127.0.0.1:18976", "Local client address")
	dataDir := flag.String("data-dir", "", "Local settings and pending submission directory")
	flag.Parse()
	host, _, err := net.SplitHostPort(*listen)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		return errors.New("客户端只能监听本机回环地址")
	}
	if *dataDir == "" {
		dir, err := os.UserConfigDir()
		if err != nil {
			return err
		}
		*dataDir = filepath.Join(dir, "Blue3-ScoreEntry")
	}
	listener, err := net.Listen("tcp", *listen)
	if err != nil {
		probe := &http.Client{Timeout: 2 * time.Second}
		if response, requestErr := probe.Get("http://" + *listen + "/api/health"); requestErr == nil {
			var health struct {
				Service string `json:"service"`
				Version string `json:"version"`
			}
			_ = json.NewDecoder(response.Body).Decode(&health)
			response.Body.Close()
			if health.Service == "blue3-score-entry-client" {
				if health.Version != clientVersion {
					return errors.New("旧版录分客户端仍在运行. 请先确认成绩已提交, 在旧页面点击右上角的退出客户端, 再打开新版 EXE.")
				}
				if !*noBrowser {
					return openBrowser("http://" + *listen)
				}
				return errors.New("客户端已经在运行")
			}
		}
		listener, err = net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return err
		}
	}
	defer listener.Close()
	app, err := newApplication(*dataDir)
	if err != nil {
		return err
	}
	app.host = listener.Addr().String()
	server := &http.Server{Handler: app, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second}
	shutdownDone := make(chan struct{})
	var shutdownOnce sync.Once
	app.shutdown = func() {
		shutdownOnce.Do(func() {
			defer close(shutdownDone)
			ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
			defer cancel()
			_ = server.Shutdown(ctx)
		})
	}
	address := "http://" + app.host
	fmt.Println("Blue3 Score Entry:", address)
	fmt.Println("在页面右上角点击退出客户端, 可关闭本程序.")
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt)
	defer signal.Stop(signals)
	go func() {
		<-signals
		app.shutdown()
	}()
	if !*noBrowser {
		go func() {
			time.Sleep(250 * time.Millisecond)
			if err := openBrowser(address); err != nil {
				showError("请在浏览器打开 " + address)
			}
		}()
	}
	if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	<-shutdownDone
	return nil
}

func main() {
	if err := run(); err != nil {
		showError(err.Error())
		os.Exit(1)
	}
}

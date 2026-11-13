package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func localRequest(t *testing.T, app *application, path string, value any) (int, map[string]any) {
	t.Helper()
	var body []byte
	method := "GET"
	if value != nil {
		body, _ = json.Marshal(value)
		method = "POST"
	}
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	req.Header.Set("X-Client-Token", app.token)
	recorder := httptest.NewRecorder()
	app.ServeHTTP(recorder, req)
	var result map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatal(err, recorder.Body.String())
	}
	return recorder.Code, result
}

func newTestApp(t *testing.T, dir string) *application {
	t.Helper()
	app, err := newApplication(dir)
	if err != nil {
		t.Fatal(err)
	}
	return app
}

func testPayload() map[string]any {
	return map[string]any{
		"server_id": "server-001", "competition_id": 13, "played_at": "2026-11-13T20:00", "rate": "1",
		"request_key": "entry-test-001", "entries": []any{
			map[string]any{"player_id": 1, "base": "100", "battle_count": "8"},
			map[string]any{"player_id": 2, "base": "-100", "battle_count": "8"},
		},
	}
}

func testBackend(t *testing.T, save http.HandlerFunc) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer entry-code" {
			sendJSON(w, 401, map[string]string{"error": "录分码不正确"})
			return
		}
		switch r.URL.Path {
		case "/api/entry/options":
			sendJSON(w, 200, map[string]any{"ok": true, "api_version": 1, "server_id": "server-001",
				"players":      []any{map[string]any{"id": 1, "name": "蓝白碗"}},
				"competitions": []any{map[string]any{"id": 13, "name": "Season 13"}}})
		case "/api/entry/matches":
			save(w, r)
		default:
			sendJSON(w, 404, map[string]string{"error": "unknown route"})
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func connectApp(t *testing.T, app *application, address string) {
	t.Helper()
	status, body := localRequest(t, app, "/api/connect", map[string]string{"server_url": address, "access_code": "entry-code"})
	if status != 200 {
		t.Fatal(status, body)
	}
}

func TestEmbeddedPagesAndScripts(t *testing.T) {
	app := newTestApp(t, t.TempDir())
	for _, path := range []string{"/", "/app.js", "/app.css", "/form.js", "/favicon.svg"} {
		recorder := httptest.NewRecorder()
		app.ServeHTTP(recorder, httptest.NewRequest("GET", path, nil))
		if recorder.Code != 200 || recorder.Body.Len() < 30 {
			t.Errorf("%s returned %d", path, recorder.Code)
		}
	}
}

func TestConnectAndSave(t *testing.T) {
	var received map[string]any
	backend := testBackend(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&received)
		sendJSON(w, 201, map[string]any{"ok": true, "id": 24, "participants": 2})
	})
	dir := t.TempDir()
	app := newTestApp(t, dir)
	connectApp(t, app, backend.URL)
	payload := testPayload()
	status, body := localRequest(t, app, "/api/submit", payload)
	if status != 201 || body["id"] != float64(24) || received["rate"] != "1" {
		t.Fatal(status, body, received)
	}
	if app.pending != nil {
		t.Fatal("successful submission left pending data")
	}
	if _, err := os.Stat(filepath.Join(dir, "pending.json")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("successful submission left a pending file")
	}
	settings, _ := os.ReadFile(filepath.Join(dir, "settings.json"))
	if bytes.Contains(settings, []byte("entry-code")) {
		t.Fatal("entry code must not be saved to the settings file")
	}
}

type transportFunc func(*http.Request) (*http.Response, error)

func (fn transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return fn(r) }

func TestUncertainSubmitSurvivesRestartAndReusesOriginalKey(t *testing.T) {
	var mu sync.Mutex
	var requests []map[string]any
	backend := testBackend(t, func(w http.ResponseWriter, r *http.Request) {
		var value map[string]any
		_ = json.NewDecoder(r.Body).Decode(&value)
		mu.Lock()
		requests = append(requests, value)
		mu.Unlock()
		sendJSON(w, 201, map[string]any{"ok": true, "id": 31, "participants": 2})
	})
	dir := t.TempDir()
	app := newTestApp(t, dir)
	connectApp(t, app, backend.URL)
	app.remote.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		response, err := http.DefaultTransport.RoundTrip(r)
		if err != nil {
			return nil, err
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		return nil, errors.New("response lost after server committed")
	})
	payload := testPayload()
	status, body := localRequest(t, app, "/api/submit", payload)
	if status != 502 || body["pending"] != true || app.pending == nil {
		t.Fatal(status, body)
	}
	restarted := newTestApp(t, dir)
	if restarted.pending == nil {
		t.Fatal("pending request was lost after restart")
	}
	connectApp(t, restarted, backend.URL)
	status, body = localRequest(t, restarted, "/api/submit", payload)
	if status != 201 || body["id"] != float64(31) || restarted.pending != nil {
		t.Fatal(status, body)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(requests) != 2 || requests[0]["request_key"] != requests[1]["request_key"] {
		t.Fatal("retry did not reuse original request", requests)
	}
}

func TestDefinitiveFailureAllowsCorrection(t *testing.T) {
	backend := testBackend(t, func(w http.ResponseWriter, r *http.Request) {
		sendJSON(w, 400, map[string]string{"error": "基础分合计必须为 0"})
	})
	app := newTestApp(t, t.TempDir())
	connectApp(t, app, backend.URL)
	status, body := localRequest(t, app, "/api/submit", testPayload())
	if status != 400 || body["pending"] != false || app.pending != nil {
		t.Fatal(status, body)
	}
}

func TestUnconfirmedSubmissionCannotBeReplacedOrSentElsewhere(t *testing.T) {
	backend := testBackend(t, func(w http.ResponseWriter, r *http.Request) {
		sendJSON(w, 503, map[string]string{"error": "暂时忙碌"})
	})
	app := newTestApp(t, t.TempDir())
	connectApp(t, app, backend.URL)
	localRequest(t, app, "/api/submit", testPayload())
	changed := testPayload()
	changed["request_key"] = "another-request-001"
	status, body := localRequest(t, app, "/api/submit", changed)
	if status != 409 || body["pending"] != true {
		t.Fatal(status, body)
	}
	status, _ = localRequest(t, app, "/api/connect", map[string]string{"server_url": "http://127.0.0.1:1", "access_code": "entry-code"})
	if status != 409 {
		t.Fatal("changed server while submission unresolved", status)
	}
}

func TestInvalidResponseKeepsSubmissionForRetry(t *testing.T) {
	backend := testBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.Write([]byte("<html>connection interrupted</html>"))
	})
	app := newTestApp(t, t.TempDir())
	connectApp(t, app, backend.URL)
	status, body := localRequest(t, app, "/api/submit", testPayload())
	if status != 502 || body["pending"] != true || app.pending == nil {
		t.Fatal(status, body)
	}
}

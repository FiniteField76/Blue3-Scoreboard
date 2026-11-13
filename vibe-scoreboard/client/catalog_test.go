package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"
)

func TestRemoteClientCannotCreateCatalogItems(t *testing.T) {
	app := newTestApp(t, t.TempDir())
	backend := testBackend(t, func(http.ResponseWriter, *http.Request) { t.Fatal("unexpected backend mutation") })
	connectApp(t, app, backend.URL)
	for _, kind := range []string{"players", "competitions"} {
		status, _ := localRequest(t, app, "/api/catalog/"+kind, map[string]string{"server_id": "server-001", "name": "new"})
		if status != 404 {
			t.Fatalf("%s creation is still available: %d", kind, status)
		}
	}
	_, health := localRequest(t, app, "/api/health", nil)
	if health["version"] != clientVersion {
		t.Fatal("missing runtime version", health)
	}
}

func TestFreshRemoteSubmissionAlwaysUsesRateOne(t *testing.T) {
	for _, rate := range []any{nil, "1", "10", 2, "1.25", "invalid"} {
		backend := testBackend(t, func(w http.ResponseWriter, r *http.Request) {
			var received map[string]any
			if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
				t.Fatal(err)
			}
			if received["rate"] != "1" {
				t.Error("non-default remote rate", received["rate"])
			}
			sendJSON(w, 503, map[string]string{"error": "temporary failure"})
		})
		app := newTestApp(t, t.TempDir())
		connectApp(t, app, backend.URL)
		payload := testPayload()
		if rate == nil {
			delete(payload, "rate")
		} else {
			payload["rate"] = rate
		}
		status, body := localRequest(t, app, "/api/submit", payload)
		if status != 503 || body["pending"] != true || app.pending == nil {
			t.Fatal(status, body)
		}
		var saved map[string]any
		if err := json.Unmarshal(app.pending.Payload, &saved); err != nil {
			t.Fatal(err)
		}
		if saved["rate"] != "1" {
			t.Fatal("retry did not preserve normalized rate", saved)
		}
	}
}

func TestLegacyPendingSubmissionKeepsOriginalRate(t *testing.T) {
	var received map[string]any
	backend := testBackend(t, func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Fatal(err)
		}
		sendJSON(w, 201, map[string]any{"ok": true, "id": 24, "participants": 2})
	})
	dir := t.TempDir()
	payload := testPayload()
	payload["rate"] = "2"
	raw, _ := json.Marshal(payload)
	pending := pendingSubmission{ServerURL: backend.URL, Payload: raw}
	if err := atomicJSON(filepath.Join(dir, "pending.json"), pending); err != nil {
		t.Fatal(err)
	}
	app := newTestApp(t, dir)
	connectApp(t, app, backend.URL)
	status, body := localRequest(t, app, "/api/submit", payload)
	if status != 201 || received["rate"] != "2" || app.pending != nil {
		t.Fatal(status, body, received)
	}
	if _, err := os.Stat(filepath.Join(dir, "pending.json")); !os.IsNotExist(err) {
		t.Fatal("retry file remains", err)
	}
	payload["request_key"] = "fresh-round-001"
	status, body = localRequest(t, app, "/api/submit", payload)
	if status != 201 || received["rate"] != "1" {
		t.Fatal(status, body, received)
	}
}

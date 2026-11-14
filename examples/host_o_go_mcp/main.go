// Minimal stdio MCP server in Go (no third-party dependencies).
//
// It supports just enough for the YAI MCP Client example:
// initialize, tools/list, tools/call (lookup_order), ping.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

type request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

func reply(id json.RawMessage, result any) {
	payload := map[string]any{"jsonrpc": "2.0", "result": result}
	if len(id) > 0 {
		payload["id"] = json.RawMessage(id)
	}
	encoded, _ := json.Marshal(payload)
	fmt.Fprintln(os.Stdout, string(encoded))
}

func fail(id json.RawMessage, code int, message string) {
	payload := map[string]any{
		"jsonrpc": "2.0",
		"error":   map[string]any{"code": code, "message": message},
	}
	if len(id) > 0 {
		payload["id"] = json.RawMessage(id)
	}
	encoded, _ := json.Marshal(payload)
	fmt.Fprintln(os.Stdout, string(encoded))
}

func lookupOrder(orderID string) string {
	orders := map[string]string{
		"SO-1001": "SO-1001 · paid · 2 items · total ¥268.00",
		"SO-1002": "SO-1002 · shipped · 1 item · total ¥99.00",
	}
	if value, ok := orders[orderID]; ok {
		return value
	}
	return "order " + orderID + " not found"
}

func main() {
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 0, 1<<20), 4<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var req request
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			continue
		}
		switch req.Method {
		case "initialize":
			reply(req.ID, map[string]any{
				"protocolVersion": "2025-06-18",
				"capabilities":    map[string]any{"tools": map[string]any{}},
				"serverInfo":      map[string]any{"name": "go-order-demo", "version": "0.1.0"},
			})
		case "notifications/initialized":
			// notification: no response
		case "ping":
			reply(req.ID, map[string]any{})
		case "tools/list":
			reply(req.ID, map[string]any{"tools": []map[string]any{{
				"name":        "lookup_order",
				"description": "Look up a demo order by id (offline).",
				"inputSchema": map[string]any{
					"type":       "object",
					"properties": map[string]any{"order_id": map[string]any{"type": "string"}},
					"required":   []string{"order_id"},
				},
			}}})
		case "tools/call":
			var params struct {
				Name      string         `json:"name"`
				Arguments map[string]any `json:"arguments"`
			}
			if err := json.Unmarshal(req.Params, &params); err != nil {
				fail(req.ID, -32602, "invalid params")
				continue
			}
			if params.Name != "lookup_order" {
				fail(req.ID, -32601, "unknown tool")
				continue
			}
			orderID, _ := params.Arguments["order_id"].(string)
			reply(req.ID, map[string]any{
				"content": []map[string]any{{
					"type": "text",
					"text": lookupOrder(orderID),
				}},
				"isError": false,
			})
		default:
			fail(req.ID, -32601, "method not found")
		}
	}
}

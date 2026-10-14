package transfer

import "testing"

func TestXDND69EncryptionFailureAbortsSendInitialization(t *testing.T) {
	t.Setenv("KITTY_PUBLIC_KEY", "")
	m := &SendManager{request_id: "request", bypass: "secret"}
	m.initialize()
	if m.bypass != "secret" {
		t.Fatalf("unprotected bypass value was modified or retained for sending: %q", m.bypass)
	}
	if m.prefix != "" || m.fid_map != nil {
		t.Fatal("send state was initialized despite bypass encryption failure")
	}
}

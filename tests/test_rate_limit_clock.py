import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from obsidian_vault_mcp import rate_limit as rl

@pytest.fixture(autouse=True)
def clean_limits():
 rl.reset_rate_limits()
 yield
 rl.reset_rate_limits()

def test_wall_clock_backward_and_forward_do_not_extend_window(monkeypatch):
 now=[100.0]
 monkeypatch.setattr(rl.time,'monotonic',lambda:now[0])
 monkeypatch.setattr(rl.time,'time',lambda:9000.0)
 rl.check_rate_limit('tool_write','synthetic',1)
 monkeypatch.setattr(rl.time,'time',lambda:1.0)
 now[0]=130.0
 with pytest.raises(rl.RateLimitExceeded) as e:rl.check_rate_limit('tool_write','synthetic',1)
 assert e.value.retry_after_seconds==30
 monkeypatch.setattr(rl.time,'time',lambda:9999999999.0)
 now[0]=159.9
 with pytest.raises(rl.RateLimitExceeded) as e:rl.check_rate_limit('tool_write','synthetic',1)
 assert e.value.retry_after_seconds==1
 now[0]=160.0
 rl.check_rate_limit('tool_write','synthetic',1)

def test_denied_retries_do_not_consume_quota_or_renew_window(monkeypatch):
 now=[0.0];monkeypatch.setattr(rl.time,'monotonic',lambda:now[0])
 rl.check_rate_limit('tool_write','synthetic',1)
 for instant in (10.,20.,30.,59.9):
  now[0]=instant
  with pytest.raises(rl.RateLimitExceeded):rl.check_rate_limit('tool_write','synthetic',1)
 assert list(rl._rate_buckets[('tool_write','synthetic')])==[0.0]
 now[0]=60.0;rl.check_rate_limit('tool_write','synthetic',1)

def test_scopes_and_principals_have_independent_windows(monkeypatch):
 monkeypatch.setattr(rl.time,'monotonic',lambda:0.0)
 rl.check_rate_limit('tool_write','a',1)
 rl.check_rate_limit('tool_read','a',1)
 rl.check_rate_limit('tool_write','b',1)
 with pytest.raises(rl.RateLimitExceeded):rl.check_rate_limit('tool_write','a',1)

def test_parallel_calls_cannot_exceed_limit(monkeypatch):
 monkeypatch.setattr(rl.time,'monotonic',lambda:0.0)
 def attempt(_):
  try:rl.check_rate_limit('tool_write','synthetic',4);return True
  except rl.RateLimitExceeded:return False
 with ThreadPoolExecutor(max_workers=8) as pool:assert sum(pool.map(attempt,range(32)))==4

def test_retry_after_tracks_oldest_remaining_admission(monkeypatch):
 now=[0.0];monkeypatch.setattr(rl.time,'monotonic',lambda:now[0])
 for instant in (10.,20.,30.):now[0]=instant;rl.check_rate_limit('tool_write','synthetic',3)
 now[0]=40.
 with pytest.raises(rl.RateLimitExceeded) as e:rl.check_rate_limit('tool_write','synthetic',3)
 assert e.value.retry_after_seconds==30
 now[0]=70.;rl.check_rate_limit('tool_write','synthetic',3)
 with pytest.raises(rl.RateLimitExceeded) as e:rl.check_rate_limit('tool_write','synthetic',3)
 assert e.value.retry_after_seconds==10

def test_tool_error_is_structured_and_log_contains_no_token(monkeypatch,caplog):
 from obsidian_vault_mcp import server
 monkeypatch.setattr(rl.time,'monotonic',lambda:0.0)
 p=rl.set_current_auth_principal('synthetic-sensitive-token')
 m=rl.set_current_request_metadata({'client_family':'pytest'})
 try:
  assert server._tool_rate_limit_error('write',1) is None
  result=json.loads(server._tool_rate_limit_error('write',1))
 finally:rl.reset_current_request_metadata(m);rl.reset_current_auth_principal(p)
 assert result['retry_after_seconds']==60 and result['scope']=='tool_write'
 assert result['error_code']=='rate_limited'
 assert 'synthetic-sensitive-token' not in caplog.text
 assert 'client=pytest' in caplog.text

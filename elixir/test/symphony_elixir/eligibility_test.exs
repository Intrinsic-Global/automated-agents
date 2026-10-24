defmodule SymphonyElixir.EligibilityTest do
  use ExUnit.Case, async: false

  alias SymphonyElixir.Eligibility
  alias SymphonyElixir.Tracker.Issue

  setup do
    old_enforce = System.get_env("SYMPHONY_ELIGIBILITY_ENFORCE")
    old_script = System.get_env("SYMPHONY_ELIGIBILITY_SCRIPT")
    old_config = System.get_env("SYMPHONY_REPO_CONFIG")

    System.put_env("SYMPHONY_ELIGIBILITY_ENFORCE", "1")
    System.put_env("SYMPHONY_REPO_CONFIG", "/tmp/does-not-matter.json")

    on_exit(fn ->
      restore("SYMPHONY_ELIGIBILITY_ENFORCE", old_enforce)
      restore("SYMPHONY_ELIGIBILITY_SCRIPT", old_script)
      restore("SYMPHONY_REPO_CONFIG", old_config)
    end)

    :ok
  end

  test "returns eligible results" do
    script = script_with(~s|import json; print(json.dumps({"status":"eligible","reasons":[]})); raise SystemExit(0)|)
    System.put_env("SYMPHONY_ELIGIBILITY_SCRIPT", script)

    assert {:ok, %{"status" => "eligible"}} = Eligibility.check(issue(), "dispatch")
  end

  test "returns blocked results with reasons" do
    script =
      script_with(~s|import json; print(json.dumps({"status":"blocked","reasons":["dependency_receipt_missing:PRO-80"]})); raise SystemExit(1)|)

    System.put_env("SYMPHONY_ELIGIBILITY_SCRIPT", script)

    assert {:ok, %{"status" => "blocked", "reasons" => ["dependency_receipt_missing:PRO-80"]}} =
             Eligibility.check(issue(), "resume")
  end

  test "fails closed on unparseable output" do
    script = script_with(~s|print("not json"); raise SystemExit(1)|)
    System.put_env("SYMPHONY_ELIGIBILITY_SCRIPT", script)

    assert {:error, {:eligibility_unparseable, _}} = Eligibility.check(issue(), "dispatch")
  end

  test "searches past malformed output for a valid JSON result" do
    script = script_with(~s|print('prefix {bad} then {"status":"eligible"}')|)
    System.put_env("SYMPHONY_ELIGIBILITY_SCRIPT", script)

    assert {:ok, %{"status" => "eligible"}} = Eligibility.check(issue(), "dispatch")
  end

  test "returns invalid results and output without a decodable JSON object" do
    invalid_result = script_with(~s|import json; print(json.dumps({"message":"missing status"}))|)
    System.put_env("SYMPHONY_ELIGIBILITY_SCRIPT", invalid_result)

    assert {:error, {:eligibility_invalid_result, %{"message" => "missing status"}}} =
             Eligibility.check(issue(), "dispatch")

    malformed_result = script_with(~s|print("{not json")|)
    System.put_env("SYMPHONY_ELIGIBILITY_SCRIPT", malformed_result)

    assert {:error, {:eligibility_unparseable, _}} = Eligibility.check(issue(), "dispatch")
  end

  test "uses script and repo config defaults from the current directory" do
    test_root = Path.join(System.tmp_dir!(), "eligibility-defaults-#{System.unique_integer([:positive])}")
    script = Path.join(test_root, "scripts/symphony_eligibility.py")
    expected_config = Path.join(test_root, "symphony-repos.json")
    File.mkdir_p!(Path.dirname(script))
    File.write!(script, "import json, sys\nprint(json.dumps({'status': 'eligible', 'config': sys.argv[2]}))\n")

    try do
      System.delete_env("SYMPHONY_ELIGIBILITY_SCRIPT")
      System.delete_env("SYMPHONY_REPO_CONFIG")

      assert {:ok, %{"status" => "eligible", "config" => ^expected_config}} =
               File.cd!(test_root, fn -> Eligibility.check(issue(), "stage") end)
    after
      File.rm_rf!(test_root)
    end
  end

  test "fails closed when python3 cannot be started" do
    original_path = System.get_env("PATH")
    empty_path = Path.join(System.tmp_dir!(), "eligibility-empty-path-#{System.unique_integer([:positive])}")
    File.mkdir_p!(empty_path)

    try do
      System.put_env("PATH", empty_path)
      assert {:error, {:eligibility_exec_failed, _}} = Eligibility.check(issue(), "dispatch")
    after
      restore("PATH", original_path)
      File.rm_rf!(empty_path)
    end
  end

  test "defaults enforcement to enabled and rejects invalid issue values" do
    System.delete_env("SYMPHONY_ELIGIBILITY_ENFORCE")
    assert Eligibility.enforce?()

    assert {:error, :invalid_eligibility_request} =
             Eligibility.check(%Issue{identifier: nil}, "dispatch")
  end

  test "can be disabled for isolated test environments" do
    System.put_env("SYMPHONY_ELIGIBILITY_ENFORCE", "0")

    assert {:ok, %{"status" => "eligible", "skipped" => true}} =
             Eligibility.check(issue(), "dispatch")
  end

  defp issue do
    %Issue{id: "issue-id", identifier: "PRO-TEST", title: "Test", state: "Todo"}
  end

  defp script_with(body) do
    path = Path.join(System.tmp_dir!(), "eligibility-test-#{System.unique_integer([:positive])}.sh")
    File.write!(path, "#!/usr/bin/env python3\n" <> body <> "\n")
    File.chmod!(path, 0o755)

    on_exit(fn -> File.rm(path) end)
    path
  end

  defp restore(key, nil), do: System.delete_env(key)
  defp restore(key, value), do: System.put_env(key, value)
end

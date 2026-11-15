defmodule SymphonyElixir.DeterministicLifecycleTest.MockRunner do
  @moduledoc false

  alias SymphonyElixir.Tracker.Issue
  alias SymphonyElixir.Workspace

  @spec run(Issue.t(), pid() | nil, keyword()) :: :ok | no_return()
  def run(%Issue{} = issue, _recipient, opts) do
    parent = Keyword.fetch!(opts, :test_pid)
    counter = Keyword.fetch!(opts, :counter)
    {:ok, workspace} = Workspace.create_for_issue(issue)
    :ok = Workspace.run_before_run_hook(workspace, issue)

    try do
      case issue.state do
        "In Progress" -> code(issue, workspace, parent, counter)
        "In Review" -> review(issue, workspace, parent)
      end
    after
      Workspace.run_after_run_hook(workspace, issue)
    end
  end

  defp code(issue, workspace, parent, counter) do
    run = Agent.get_and_update(counter, &{&1 + 1, &1 + 1})
    value = if run == 1, do: "wrong", else: "#{issue.identifier}:fixture-nonce"
    File.write!(Path.join(workspace, "fixture.txt"), value)
    ensure_jj_repo!(workspace)
    {_, 0} = System.cmd("jj", ["describe", "-m", "#{issue.identifier} candidate #{run}"], cd: workspace)
    {_, 0} = System.cmd("jj", ["new"], cd: workspace)
    {candidate, 0} = System.cmd("jj", ["log", "-r", "@-", "--no-graph", "-T", "change_id"], cd: workspace)
    put_issue_state(issue, "In Review")
    send(parent, {:mock_coder_candidate, issue.identifier, String.trim(candidate), run})
  end

  defp review(issue, workspace, parent) do
    {candidate, 0} = System.cmd("jj", ["log", "-r", "@-", "--no-graph", "-T", "change_id"], cd: workspace)
    content = File.read!(Path.join(workspace, "fixture.txt"))
    accepted? = content == "#{issue.identifier}:fixture-nonce"
    put_issue_state(issue, if(accepted?, do: "Done", else: "In Progress"))
    send(parent, {:mock_reviewer_verdict, issue.identifier, String.trim(candidate), accepted?})
  end

  defp ensure_jj_repo!(workspace) do
    unless File.dir?(Path.join(workspace, ".jj")) do
      {_, 0} = System.cmd("jj", ["git", "init", "--colocate", workspace])
    end
  end

  defp put_issue_state(issue, state) do
    issues = Application.fetch_env!(:symphony_elixir, :memory_tracker_issues)
    updated = Enum.map(issues, fn entry -> if entry.id == issue.id, do: %{entry | state: state}, else: entry end)
    Application.put_env(:symphony_elixir, :memory_tracker_issues, updated)
  end
end

defmodule SymphonyElixir.DeterministicLifecycleTest do
  use SymphonyElixir.TestSupport

  defmodule NoProgressRunner do
    @moduledoc false
    alias SymphonyElixir.Tracker.Issue
    alias SymphonyElixir.Workspace

    @spec run(Issue.t(), pid() | nil, keyword()) :: :ok
    def run(%Issue{} = issue, _recipient, opts) do
      {:ok, _workspace} = Workspace.create_for_issue(issue)
      send(Keyword.fetch!(opts, :test_pid), {:no_progress_run, issue.identifier})
      :ok
    end
  end

  test "real orchestrator retries a rejected candidate, repairs it, and independently verifies the new JJ change" do
    suffix = System.unique_integer([:positive])
    root = Path.join(System.tmp_dir!(), "symphony-deterministic-#{suffix}")
    workspace_root = Path.join(root, "workspaces")
    File.mkdir_p!(root)
    previous_issues = Application.get_env(:symphony_elixir, :memory_tracker_issues)

    issue = %Issue{
      id: "deterministic-#{suffix}",
      identifier: "SMOKE-#{suffix}",
      title: "Deterministic two-lane smoke",
      description: "Write the fixture with nonce fixture-nonce",
      state: "In Progress",
      url: "https://example.invalid/issues/SMOKE-#{suffix}",
      labels: [],
      dispatchable: true
    }

    Application.put_env(:symphony_elixir, :memory_tracker_issues, [issue])

    write_workflow_file!(Workflow.workflow_file_path(),
      tracker_kind: "memory",
      workspace_root: workspace_root,
      tracker_active_states: ["In Progress", "In Review"],
      poll_interval_ms: 25
    )

    counter = start_supervised!({Agent, fn -> 0 end})
    task_supervisor = start_supervised!({Task.Supervisor, name: nil})
    orchestrator_name = Module.concat(__MODULE__, "Orchestrator#{suffix}")

    {:ok, orchestrator} =
      Orchestrator.start_link(
        name: orchestrator_name,
        task_supervisor: task_supervisor,
        agent_runner: SymphonyElixir.DeterministicLifecycleTest.MockRunner,
        runner_opts: [test_pid: self(), counter: counter]
      )

    on_exit(fn ->
      if Process.alive?(orchestrator), do: GenServer.stop(orchestrator, :normal)

      if is_nil(previous_issues) do
        Application.delete_env(:symphony_elixir, :memory_tracker_issues)
      else
        Application.put_env(:symphony_elixir, :memory_tracker_issues, previous_issues)
      end

      File.rm_rf(root)
    end)

    identifier = issue.identifier
    assert_receive {:mock_coder_candidate, ^identifier, first_candidate, 1}, 10_000
    assert_receive {:mock_reviewer_verdict, ^identifier, ^first_candidate, false}, 10_000
    assert_receive {:mock_coder_candidate, ^identifier, repaired_candidate, 2}, 10_000
    refute repaired_candidate == first_candidate
    assert_receive {:mock_reviewer_verdict, ^identifier, ^repaired_candidate, true}, 10_000
    assert Agent.get(counter, & &1) == 2

    workspace = Path.join(workspace_root, identifier)
    assert File.read!(Path.join(workspace, "fixture.txt")) == "#{identifier}:fixture-nonce"
    assert {:ok, [completed_issue]} = Tracker.fetch_issues_by_ids([issue.id])
    assert completed_issue.state == "Done"
  end

  test "continuation budget survives orchestrator process restart" do
    identifier = "BUDGET-#{System.unique_integer([:positive])}"
    assert {:ok, 1} = Workspace.record_continuation_attempt(identifier)
    assert {:ok, 2} = Workspace.record_continuation_attempt(identifier)
    assert {:ok, 3} = Workspace.record_continuation_attempt(identifier)
    assert {:ok, 4} = Workspace.record_continuation_attempt(identifier)
    assert {:ok, 1} = Workspace.record_failure_attempt(identifier)

    first_name = Module.concat(__MODULE__, "BudgetFirst")
    {:ok, first} = Orchestrator.start_link(name: first_name)
    GenServer.stop(first)

    second_name = Module.concat(__MODULE__, "BudgetSecond")
    {:ok, second} = Orchestrator.start_link(name: second_name)
    GenServer.stop(second)

    assert Workspace.continuation_attempts(identifier) == 4
    assert Workspace.failure_attempts(identifier) == 1

    issue = %Issue{
      id: identifier,
      identifier: identifier,
      title: "Still active",
      state: "In Progress",
      dispatchable: true
    }

    blocked =
      Orchestrator.handle_retry_issue_lookup_for_test(
        issue,
        %Orchestrator.State{},
        identifier,
        1,
        %{identifier: identifier}
      )

    assert %{error: error} = blocked.blocked[identifier]
    assert error =~ "continuation limit reached"
    assert :ok = Workspace.clear_continuation_attempts(identifier)
    assert :ok = Workspace.clear_failure_attempts(identifier)
  end

  test "unchanged active work stops after the configured continuation budget" do
    suffix = System.unique_integer([:positive])
    workspace_root = Path.join(System.tmp_dir!(), "symphony-no-progress-#{suffix}")
    identifier = "STALL-#{suffix}"

    issue = %Issue{
      id: "no-progress-#{suffix}",
      identifier: identifier,
      title: "No progress fixture",
      description: "Remain unchanged",
      state: "In Progress",
      labels: [],
      dispatchable: true
    }

    previous_issues = Application.get_env(:symphony_elixir, :memory_tracker_issues)
    Application.put_env(:symphony_elixir, :memory_tracker_issues, [issue])

    write_workflow_file!(Workflow.workflow_file_path(),
      tracker_kind: "memory",
      workspace_root: workspace_root,
      max_continuation_retries: 1,
      poll_interval_ms: 25
    )

    task_supervisor = start_supervised!({Task.Supervisor, name: nil})
    orchestrator_name = Module.concat(__MODULE__, "NoProgressOrchestrator#{suffix}")

    {:ok, orchestrator} =
      Orchestrator.start_link(
        name: orchestrator_name,
        task_supervisor: task_supervisor,
        agent_runner: NoProgressRunner,
        runner_opts: [test_pid: self()]
      )

    on_exit(fn ->
      if Process.alive?(orchestrator), do: GenServer.stop(orchestrator, :normal)

      if is_nil(previous_issues) do
        Application.delete_env(:symphony_elixir, :memory_tracker_issues)
      else
        Application.put_env(:symphony_elixir, :memory_tracker_issues, previous_issues)
      end

      File.rm_rf(workspace_root)
    end)

    assert_receive {:no_progress_run, ^identifier}, 10_000
    assert_receive {:no_progress_run, ^identifier}, 10_000
    refute_receive {:no_progress_run, ^identifier}, 1_500

    snapshot = GenServer.call(orchestrator, :snapshot)
    assert [%{identifier: ^identifier, error: reason}] = snapshot.blocked
    assert reason =~ "continuation limit reached"
  end
end

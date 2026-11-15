defmodule SymphonyElixir.Workspace do
  @moduledoc """
  Creates isolated per-issue workspaces for parallel Codex agents.
  """

  require Logger
  alias SymphonyElixir.{Config, PathSafety, SSH}

  @remote_workspace_marker "__SYMPHONY_WORKSPACE__"
  @failed_bootstrap_marker ".symphony-bootstrap-failed"
  @quarantine_marker ".symphony-quarantined"
  @quarantine_directory ".symphony-quarantine"

  @type worker_host :: String.t() | nil

  @spec create_for_issue(map() | String.t() | nil, worker_host()) ::
          {:ok, Path.t()} | {:error, term()}
  def create_for_issue(issue_or_identifier, worker_host \\ nil) do
    issue_context = issue_context(issue_or_identifier)

    try do
      safe_id = workspace_key(issue_or_identifier)

      with :ok <- ensure_not_quarantined(safe_id),
           {:ok, workspace} <- workspace_path_for_issue(safe_id, worker_host),
           :ok <- validate_workspace_path(workspace, worker_host),
           {:ok, workspace, created?} <- ensure_workspace(workspace, worker_host) do
        case maybe_run_after_create_hook(workspace, issue_context, created?, worker_host) do
          :ok ->
            {:ok, workspace}

          {:error, _reason} = error ->
            if created? do
              mark_workspace_bootstrap_failed(workspace, worker_host)
              Logger.warning("Preserving workspace after creation hook failure #{issue_log_context(issue_context)} workspace=#{workspace} worker_host=#{worker_host_for_log(worker_host)}")
            end

            error
        end
      end
    rescue
      error in [ArgumentError, ErlangError, File.Error] ->
        Logger.error("Workspace creation failed #{issue_log_context(issue_context)} worker_host=#{worker_host_for_log(worker_host)} error=#{Exception.message(error)}")
        {:error, error}
    end
  end

  @spec quarantine_issue(map() | String.t(), String.t(), worker_host()) :: :ok | {:error, term()}
  def quarantine_issue(issue_or_identifier, _reason, worker_host) do
    safe_id = workspace_key(issue_or_identifier)

    with :ok <- persist_issue_quarantine(safe_id),
         {:ok, workspace} <- workspace_path_for_issue(safe_id, worker_host) do
      quarantine_workspace(workspace, worker_host)
    end
  end

  @spec quarantined_issue?(map() | String.t()) :: boolean()
  def quarantined_issue?(issue_or_identifier) do
    safe_id = workspace_key(issue_or_identifier)
    File.regular?(issue_quarantine_path(safe_id))
  end

  @doc false
  @spec continuation_attempts(map() | String.t()) :: non_neg_integer()
  def continuation_attempts(issue_or_identifier) do
    read_counter(issue_continuation_path(workspace_key(issue_or_identifier)))
  end

  @doc false
  @spec record_continuation_attempt(map() | String.t(), non_neg_integer()) ::
          {:ok, non_neg_integer()} | {:error, term()}
  def record_continuation_attempt(issue_or_identifier, minimum_count \\ 0)
      when is_integer(minimum_count) and minimum_count >= 0 do
    path = issue_continuation_path(workspace_key(issue_or_identifier))
    write_counter(path, continuation_attempts(issue_or_identifier), minimum_count)
  end

  @doc false
  @spec clear_continuation_attempts(map() | String.t()) :: :ok | {:error, term()}
  def clear_continuation_attempts(issue_or_identifier) do
    remove_counter_file(issue_continuation_path(workspace_key(issue_or_identifier)))
  end

  @doc false
  @spec failure_attempts(map() | String.t()) :: non_neg_integer()
  def failure_attempts(issue_or_identifier) do
    read_counter(issue_failure_path(workspace_key(issue_or_identifier)))
  end

  @doc false
  @spec record_failure_attempt(map() | String.t(), non_neg_integer()) ::
          {:ok, non_neg_integer()} | {:error, term()}
  def record_failure_attempt(issue_or_identifier, minimum_count \\ 0)
      when is_integer(minimum_count) and minimum_count >= 0 do
    path = issue_failure_path(workspace_key(issue_or_identifier))
    write_counter(path, failure_attempts(issue_or_identifier), minimum_count)
  end

  @doc false
  @spec clear_failure_attempts(map() | String.t()) :: :ok | {:error, term()}
  def clear_failure_attempts(issue_or_identifier) do
    remove_counter_file(issue_failure_path(workspace_key(issue_or_identifier)))
  end

  defp read_counter(path) do
    case File.read(path) do
      {:ok, value} ->
        case Integer.parse(String.trim(value)) do
          {count, ""} when count >= 0 -> count
          _ -> 1_000_000_000
        end

      {:error, :enoent} ->
        0

      {:error, _reason} ->
        1_000_000_000
    end
  end

  defp write_counter(path, existing, minimum_count) do
    count = max(existing, minimum_count) + 1
    temporary = path <> ".#{System.unique_integer([:positive])}.tmp"

    with :ok <- File.mkdir_p(Path.dirname(path)),
         :ok <- File.write(temporary, "#{count}\n"),
         :ok <- File.rename(temporary, path) do
      {:ok, count}
    else
      {:error, reason} ->
        File.rm(temporary)
        {:error, reason}
    end
  end

  defp remove_counter_file(path) do
    case File.rm(path) do
      :ok -> :ok
      {:error, :enoent} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end

  defp ensure_workspace(workspace, nil) do
    cond do
      File.dir?(workspace) ->
        cond do
          File.exists?(Path.join(workspace, @quarantine_marker)) ->
            {:error, {:workspace_quarantined, workspace}}

          File.exists?(Path.join(workspace, @failed_bootstrap_marker)) ->
            {:error, {:workspace_bootstrap_incomplete, workspace}}

          true ->
            {:ok, workspace, false}
        end

      File.exists?(workspace) ->
        {:error, {:workspace_path_conflict, workspace, :not_a_directory}}

      true ->
        create_workspace(workspace)
    end
  end

  defp ensure_workspace(workspace, worker_host) when is_binary(worker_host) do
    workspace_root = Config.settings!().workspace.root

    script =
      [
        "set -eu",
        remote_shell_assign("workspace_root", workspace_root),
        remote_shell_assign("workspace", workspace),
        "mkdir -p \"$workspace_root\"",
        "workspace_root=$(cd \"$workspace_root\" && pwd -P)",
        "workspace_parent=$(dirname \"$workspace\")",
        "mkdir -p \"$workspace_parent\"",
        "workspace=\"$(cd \"$workspace_parent\" && pwd -P)/$(basename \"$workspace\")\"",
        "case \"$workspace/\" in \"$workspace_root/\"*) ;; *) echo \"workspace is outside configured root: $workspace\" >&2; exit 73 ;; esac",
        "if [ -f \"$workspace_root/#{@quarantine_directory}/$(basename \"$workspace\")\" ]; then echo \"issue is quarantined; reconcile and clear it explicitly: $workspace\" >&2; exit 75; fi",
        "if [ -L \"$workspace\" ]; then echo \"refusing symlink workspace path: $workspace\" >&2; exit 73; fi",
        "if [ -f \"$workspace/#{@quarantine_marker}\" ]; then echo \"workspace is quarantined; reconcile and clear the marker explicitly: $workspace\" >&2; exit 75; fi",
        "if [ -f \"$workspace/#{@failed_bootstrap_marker}\" ]; then echo \"workspace bootstrap previously failed; manual reconciliation required: $workspace\" >&2; exit 74; fi",
        "if [ -d \"$workspace\" ]; then",
        "  created=0",
        "elif [ -e \"$workspace\" ]; then",
        "  echo \"workspace path exists and is not a directory: $workspace\" >&2",
        "  exit 73",
        "else",
        "  mkdir -p \"$workspace\"",
        "  created=1",
        "fi",
        "cd \"$workspace\"",
        "printf '%s\\t%s\\t%s\\n' '#{@remote_workspace_marker}' \"$created\" \"$(pwd -P)\""
      ]
      |> Enum.reject(&(&1 == ""))
      |> Enum.join("\n")

    case run_remote_command(worker_host, script, Config.settings!().hooks.timeout_ms) do
      {:ok, {output, 0}} ->
        parse_remote_workspace_output(output)

      {:ok, {output, status}} ->
        {:error, {:workspace_prepare_failed, worker_host, status, output}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp create_workspace(workspace) do
    File.mkdir_p!(workspace)
    {:ok, workspace, true}
  end

  defp mark_workspace_bootstrap_failed(workspace, nil) do
    File.write(Path.join(workspace, @failed_bootstrap_marker), "after_create hook failed; manual reconciliation required\n")
  end

  defp mark_workspace_bootstrap_failed(workspace, worker_host) when is_binary(worker_host) do
    script =
      [
        remote_shell_assign("workspace", workspace),
        "printf '%s\\n' 'after_create hook failed; manual reconciliation required' > \"$workspace/#{@failed_bootstrap_marker}\""
      ]
      |> Enum.join("\n")

    run_remote_command(worker_host, script, Config.settings!().hooks.timeout_ms)
  end

  defp quarantine_workspace(workspace, nil) do
    if File.dir?(workspace) do
      case File.write(
             Path.join(workspace, @quarantine_marker),
             "Symphony quarantined this workspace; reconcile it before reuse.\n"
           ) do
        :ok -> :ok
        {:error, reason} -> {:error, reason}
      end
    else
      :ok
    end
  end

  defp quarantine_workspace(workspace, worker_host) when is_binary(worker_host) do
    script =
      [
        remote_shell_assign("workspace", workspace),
        "if [ -d \"$workspace\" ] && [ ! -L \"$workspace\" ]; then",
        "  printf '%s\\n' 'Symphony quarantined this workspace; reconcile it before reuse.' > \"$workspace/#{@quarantine_marker}\"",
        "fi"
      ]
      |> Enum.join("\n")

    case run_remote_command(worker_host, script, Config.settings!().hooks.timeout_ms) do
      {:ok, {_output, 0}} -> :ok
      {:ok, {output, status}} -> {:error, {:workspace_quarantine_failed, worker_host, status, output}}
      {:error, reason} -> {:error, reason}
    end
  end

  defp persist_issue_quarantine(safe_id) do
    quarantine_path = issue_quarantine_path(safe_id)

    case File.mkdir_p(Path.dirname(quarantine_path)) do
      :ok -> File.write(quarantine_path, "quarantined\n")
      {:error, reason} -> {:error, reason}
    end
  end

  defp ensure_not_quarantined(safe_id) do
    if File.regular?(issue_quarantine_path(safe_id)) do
      {:error, {:workspace_quarantined, safe_id}}
    else
      :ok
    end
  end

  defp issue_quarantine_path(safe_id) do
    Config.local_workspace_root()
    |> Path.join(@quarantine_directory)
    |> Path.join(safe_id)
  end

  defp issue_continuation_path(safe_id) do
    Config.local_workspace_root()
    |> Path.join(".symphony-state")
    |> Path.join("continuations")
    |> Path.join(safe_id)
  end

  defp issue_failure_path(safe_id) do
    Config.local_workspace_root()
    |> Path.join(".symphony-state")
    |> Path.join("failures")
    |> Path.join(safe_id)
  end

  @spec remove(Path.t()) :: {:ok, [String.t()]} | {:error, term(), String.t()}
  def remove(workspace), do: remove(workspace, nil)

  @spec remove(Path.t(), worker_host()) :: {:ok, [String.t()]} | {:error, term(), String.t()}
  def remove(workspace, nil) do
    case File.exists?(workspace) do
      true ->
        case validate_workspace_path(workspace, nil) do
          :ok ->
            remove_local_workspace(workspace)

          {:error, reason} ->
            {:error, reason, ""}
        end

      false ->
        File.rm_rf(workspace)
    end
  end

  def remove(workspace, worker_host) when is_binary(worker_host) do
    maybe_run_before_remove_hook(workspace, worker_host)

    script =
      [
        remote_shell_assign("workspace", workspace),
        "rm -rf \"$workspace\""
      ]
      |> Enum.join("\n")

    case run_remote_command(worker_host, script, Config.settings!().hooks.timeout_ms) do
      {:ok, {_output, 0}} ->
        {:ok, []}

      {:ok, {output, status}} ->
        {:error, {:workspace_remove_failed, worker_host, status, output}, ""}

      {:error, reason} ->
        {:error, reason, ""}
    end
  end

  @doc false
  @spec remove_recorded(Path.t(), worker_host()) :: {:ok, [String.t()]} | {:error, term(), String.t()}
  def remove_recorded(workspace, nil) when is_binary(workspace) do
    if Path.type(workspace) == :absolute do
      case validate_recorded_workspace_path(workspace) do
        :ok ->
          remove_local_workspace(workspace)

        {:error, reason} ->
          {:error, reason, ""}
      end
    else
      {:error, {:workspace_path_unreadable, workspace, :not_absolute}, ""}
    end
  end

  def remove_recorded(workspace, worker_host) when is_binary(workspace) and is_binary(worker_host) do
    remove(workspace, worker_host)
  end

  def remove_recorded(workspace, _worker_host) do
    {:error, {:workspace_path_unreadable, workspace, :invalid}, ""}
  end

  defp remove_local_workspace(workspace) do
    maybe_run_before_remove_hook(workspace, nil)
    File.rm_rf(workspace)
  end

  @spec remove_issue_workspaces(term()) :: :ok
  def remove_issue_workspaces(identifier), do: remove_issue_workspaces(identifier, nil)

  @spec remove_issue_workspaces(term(), worker_host()) :: :ok
  def remove_issue_workspaces(%{id: _issue_id, identifier: _identifier} = issue, worker_host)
      when is_binary(worker_host) do
    case workspace_path_for_issue(workspace_key(issue), worker_host) do
      {:ok, workspace} -> remove(workspace, worker_host)
      {:error, _reason} -> :ok
    end

    :ok
  end

  def remove_issue_workspaces(%{id: _issue_id, identifier: _identifier} = issue, nil) do
    case Config.settings!().worker.ssh_hosts do
      [] ->
        case workspace_path_for_issue(workspace_key(issue), nil) do
          {:ok, workspace} -> remove(workspace, nil)
          {:error, _reason} -> :ok
        end

      worker_hosts ->
        Enum.each(worker_hosts, &remove_issue_workspaces(issue, &1))
    end

    :ok
  end

  def remove_issue_workspaces(identifier, worker_host) when is_binary(identifier) and is_binary(worker_host) do
    case workspace_path_for_issue(workspace_key(identifier), worker_host) do
      {:ok, workspace} -> remove(workspace, worker_host)
      {:error, _reason} -> :ok
    end

    :ok
  end

  def remove_issue_workspaces(identifier, nil) when is_binary(identifier) do
    case Config.settings!().worker.ssh_hosts do
      [] ->
        case workspace_path_for_issue(workspace_key(identifier), nil) do
          {:ok, workspace} -> remove(workspace, nil)
          {:error, _reason} -> :ok
        end

      worker_hosts ->
        Enum.each(worker_hosts, &remove_issue_workspaces(identifier, &1))
    end

    :ok
  end

  def remove_issue_workspaces(_identifier, _worker_host), do: :ok

  @spec run_before_run_hook(Path.t(), map() | String.t() | nil, worker_host()) ::
          :ok | {:skip, term()} | {:error, term()}
  def run_before_run_hook(workspace, issue_or_identifier, worker_host \\ nil) when is_binary(workspace) do
    issue_context = issue_context(issue_or_identifier)
    hooks = Config.settings!().hooks

    case hooks.before_run do
      nil ->
        :ok

      command ->
        run_hook(command, workspace, issue_context, "before_run", worker_host)
    end
  end

  @spec run_after_run_hook(Path.t(), map() | String.t() | nil, worker_host()) :: :ok
  def run_after_run_hook(workspace, issue_or_identifier, worker_host \\ nil) when is_binary(workspace) do
    issue_context = issue_context(issue_or_identifier)
    hooks = Config.settings!().hooks

    case hooks.after_run do
      nil ->
        :ok

      command ->
        run_hook(command, workspace, issue_context, "after_run", worker_host)
        |> ignore_hook_failure()
    end
  end

  defp workspace_path_for_issue(safe_id, nil) when is_binary(safe_id) do
    Config.local_workspace_root()
    |> Path.join(safe_id)
    |> PathSafety.canonicalize()
  end

  defp workspace_path_for_issue(safe_id, worker_host) when is_binary(safe_id) and is_binary(worker_host) do
    {:ok, Path.join(Config.settings!().workspace.root, safe_id)}
  end

  @doc """
  Returns the collision-safe directory name for an issue identifier.

  The hash is derived from the original identifier so callers that only know the identifier can
  derive the same key as callers holding a full tracker issue.
  """
  @spec workspace_key(map() | String.t() | nil) :: String.t()
  def workspace_key(%{identifier: identifier}), do: workspace_key(identifier)

  def workspace_key(identifier) when is_binary(identifier) do
    safe_identifier = safe_identifier(identifier)

    if safe_identifier == identifier do
      safe_identifier
    else
      "#{safe_identifier}--#{short_identifier_hash(identifier)}"
    end
  end

  def workspace_key(_identifier), do: "issue"

  defp safe_identifier(identifier) when is_binary(identifier),
    do: String.replace(identifier, ~r/[^a-zA-Z0-9._-]/, "_")

  defp short_identifier_hash(identifier) do
    :crypto.hash(:sha256, identifier)
    |> Base.encode16(case: :lower)
    |> binary_part(0, 16)
  end

  defp maybe_run_after_create_hook(workspace, issue_context, created?, worker_host) do
    hooks = Config.settings!().hooks

    case created? do
      true ->
        case hooks.after_create do
          nil ->
            :ok

          command ->
            run_hook(command, workspace, issue_context, "after_create", worker_host)
        end

      false ->
        :ok
    end
  end

  defp maybe_run_before_remove_hook(workspace, nil) do
    hooks = Config.settings!().hooks

    case File.dir?(workspace) do
      true ->
        case hooks.before_remove do
          nil ->
            :ok

          command ->
            run_hook(
              command,
              workspace,
              %{issue_id: nil, issue_identifier: Path.basename(workspace)},
              "before_remove",
              nil
            )
            |> ignore_hook_failure()
        end

      false ->
        :ok
    end
  end

  defp maybe_run_before_remove_hook(workspace, worker_host) when is_binary(worker_host) do
    hooks = Config.settings!().hooks

    case hooks.before_remove do
      nil ->
        :ok

      command ->
        script =
          [
            remote_shell_assign("workspace", workspace),
            "if [ -d \"$workspace\" ]; then",
            "  cd \"$workspace\"",
            "  #{command}",
            "fi"
          ]
          |> Enum.join("\n")

        run_remote_command(worker_host, script, Config.settings!().hooks.timeout_ms)
        |> case do
          {:ok, {output, status}} ->
            handle_hook_command_result(
              {output, status},
              workspace,
              %{issue_id: nil, issue_identifier: Path.basename(workspace)},
              "before_remove"
            )

          {:error, {:workspace_hook_timeout, "before_remove", _timeout_ms} = reason} ->
            {:error, reason}

          {:error, reason} ->
            {:error, reason}
        end
        |> ignore_hook_failure()
    end
  end

  defp ignore_hook_failure(:ok), do: :ok
  defp ignore_hook_failure({:error, _reason}), do: :ok

  defp run_hook(command, workspace, issue_context, hook_name, nil) do
    timeout_ms = Config.settings!().hooks.timeout_ms

    Logger.info("Running workspace hook hook=#{hook_name} #{issue_log_context(issue_context)} workspace=#{workspace} worker_host=local")

    task =
      Task.async(fn ->
        System.cmd("sh", ["-lc", command], cd: workspace, stderr_to_stdout: true)
      end)

    case Task.yield(task, timeout_ms) do
      {:ok, cmd_result} ->
        handle_hook_command_result(cmd_result, workspace, issue_context, hook_name)

      nil ->
        Task.shutdown(task, :brutal_kill)

        Logger.warning("Workspace hook timed out hook=#{hook_name} #{issue_log_context(issue_context)} workspace=#{workspace} worker_host=local timeout_ms=#{timeout_ms}")

        {:error, {:workspace_hook_timeout, hook_name, timeout_ms}}
    end
  end

  defp run_hook(command, workspace, issue_context, hook_name, worker_host) when is_binary(worker_host) do
    timeout_ms = Config.settings!().hooks.timeout_ms

    Logger.info("Running workspace hook hook=#{hook_name} #{issue_log_context(issue_context)} workspace=#{workspace} worker_host=#{worker_host}")

    case run_remote_command(worker_host, "cd #{shell_escape(workspace)} && #{command}", timeout_ms) do
      {:ok, cmd_result} ->
        handle_hook_command_result(cmd_result, workspace, issue_context, hook_name)

      {:error, {:workspace_hook_timeout, ^hook_name, _timeout_ms} = reason} ->
        {:error, reason}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp handle_hook_command_result({_output, 0}, _workspace, _issue_id, _hook_name) do
    :ok
  end

  defp handle_hook_command_result({output, 75}, workspace, issue_context, "before_run") do
    sanitized_output = sanitize_hook_output_for_log(output)

    Logger.info("Workspace hook skipped run hook=before_run #{issue_log_context(issue_context)} workspace=#{workspace} output=#{inspect(sanitized_output)}")

    {:skip, {:workspace_hook_skipped, sanitized_output}}
  end

  defp handle_hook_command_result({output, status}, workspace, issue_context, hook_name) do
    sanitized_output = sanitize_hook_output_for_log(output)

    Logger.warning("Workspace hook failed hook=#{hook_name} #{issue_log_context(issue_context)} workspace=#{workspace} status=#{status} output=#{inspect(sanitized_output)}")

    {:error, {:workspace_hook_failed, hook_name, status, output}}
  end

  defp sanitize_hook_output_for_log(output, max_bytes \\ 2_048) do
    binary_output = IO.iodata_to_binary(output)

    case byte_size(binary_output) <= max_bytes do
      true ->
        binary_output

      false ->
        binary_part(binary_output, 0, max_bytes) <> "... (truncated)"
    end
  end

  defp validate_workspace_path(workspace, nil) when is_binary(workspace) do
    validate_local_workspace_path(workspace, Config.local_workspace_root())
  end

  defp validate_workspace_path(workspace, worker_host)
       when is_binary(workspace) and is_binary(worker_host) do
    cond do
      String.trim(workspace) == "" ->
        {:error, {:workspace_path_unreadable, workspace, :empty}}

      String.contains?(workspace, ["\n", "\r", <<0>>]) ->
        {:error, {:workspace_path_unreadable, workspace, :invalid_characters}}

      true ->
        :ok
    end
  end

  defp validate_recorded_workspace_path(workspace) when is_binary(workspace) do
    validate_local_workspace_path(workspace, Path.dirname(workspace))
  end

  defp validate_local_workspace_path(workspace, workspace_root)
       when is_binary(workspace) and is_binary(workspace_root) do
    expanded_workspace = Path.expand(workspace)
    expanded_root = Path.expand(workspace_root)
    expanded_root_prefix = expanded_root <> "/"

    with {:ok, canonical_workspace} <- PathSafety.canonicalize(expanded_workspace),
         {:ok, canonical_root} <- PathSafety.canonicalize(expanded_root) do
      canonical_root_prefix = canonical_root <> "/"

      cond do
        canonical_workspace == canonical_root ->
          {:error, {:workspace_equals_root, canonical_workspace, canonical_root}}

        String.starts_with?(canonical_workspace <> "/", canonical_root_prefix) ->
          :ok

        String.starts_with?(expanded_workspace <> "/", expanded_root_prefix) ->
          {:error, {:workspace_symlink_escape, expanded_workspace, canonical_root}}

        true ->
          {:error, {:workspace_outside_root, canonical_workspace, canonical_root}}
      end
    else
      {:error, {:path_canonicalize_failed, path, reason}} ->
        {:error, {:workspace_path_unreadable, path, reason}}
    end
  end

  defp remote_shell_assign(variable_name, raw_path)
       when is_binary(variable_name) and is_binary(raw_path) do
    [
      "#{variable_name}=#{shell_escape(raw_path)}",
      "case \"$#{variable_name}\" in",
      "  '~') #{variable_name}=\"$HOME\" ;;",
      "  '~/'*) " <> variable_name <> "=\"$HOME/${" <> variable_name <> "#\\~/}\" ;;",
      "esac"
    ]
    |> Enum.join("\n")
  end

  defp parse_remote_workspace_output(output) do
    lines = String.split(IO.iodata_to_binary(output), "\n", trim: true)

    payload =
      Enum.find_value(lines, fn line ->
        case String.split(line, "\t", parts: 3) do
          [@remote_workspace_marker, created, path] when created in ["0", "1"] and path != "" ->
            {created == "1", path}

          _ ->
            nil
        end
      end)

    case payload do
      {created?, workspace} when is_boolean(created?) and is_binary(workspace) ->
        {:ok, workspace, created?}

      _ ->
        {:error, {:workspace_prepare_failed, :invalid_output, output}}
    end
  end

  defp run_remote_command(worker_host, script, timeout_ms)
       when is_binary(worker_host) and is_binary(script) and is_integer(timeout_ms) and timeout_ms > 0 do
    task =
      Task.async(fn ->
        SSH.run(worker_host, script, stderr_to_stdout: true)
      end)

    case Task.yield(task, timeout_ms) do
      {:ok, result} ->
        result

      nil ->
        Task.shutdown(task, :brutal_kill)
        {:error, {:workspace_hook_timeout, "remote_command", timeout_ms}}
    end
  end

  defp shell_escape(value) when is_binary(value) do
    "'" <> String.replace(value, "'", "'\"'\"'") <> "'"
  end

  defp worker_host_for_log(nil), do: "local"
  defp worker_host_for_log(worker_host), do: worker_host

  defp issue_context(%{id: issue_id, identifier: identifier}) do
    %{
      issue_id: issue_id,
      issue_identifier: identifier || "issue"
    }
  end

  defp issue_context(identifier) when is_binary(identifier) do
    %{
      issue_id: nil,
      issue_identifier: identifier
    }
  end

  defp issue_context(_identifier) do
    %{
      issue_id: nil,
      issue_identifier: "issue"
    }
  end

  defp issue_log_context(%{issue_id: issue_id, issue_identifier: issue_identifier}) do
    "issue_id=#{issue_id || "n/a"} issue_identifier=#{issue_identifier || "issue"}"
  end
end

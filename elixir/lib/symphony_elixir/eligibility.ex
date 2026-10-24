defmodule SymphonyElixir.Eligibility do
  @moduledoc """
  Host-side wrapper around the deterministic eligibility preflight script.

  The script is the single implementation shared by dispatch, resume, review
  handoff and staging. This module only invokes it and decodes the result.
  """

  require Logger
  alias SymphonyElixir.Tracker.Issue

  @phases ["dispatch", "resume", "review", "stage"]

  @spec check(Issue.t(), String.t()) :: {:ok, map()} | {:error, term()}
  def check(%Issue{} = issue, phase) when phase in @phases and is_binary(issue.identifier) do
    if enforce?() do
      script =
        System.get_env("SYMPHONY_ELIGIBILITY_SCRIPT") ||
          Path.join(File.cwd!(), "scripts/symphony_eligibility.py")

      config =
        System.get_env("SYMPHONY_REPO_CONFIG") ||
          Path.join(File.cwd!(), "symphony-repos.json")

      args = [script, "--config", config, "check", "--issue", issue.identifier, "--phase", phase]

      try do
        {output, _status} = System.cmd("python3", args, stderr_to_stdout: true)
        decode(output)
      rescue
        error -> {:error, {:eligibility_exec_failed, Exception.message(error)}}
      end
    else
      {:ok, %{"status" => "eligible", "skipped" => true}}
    end
  end

  def check(_issue, _phase), do: {:error, :invalid_eligibility_request}

  @spec enforce?() :: boolean()
  def enforce? do
    case System.get_env("SYMPHONY_ELIGIBILITY_ENFORCE") do
      nil -> true
      value -> String.downcase(String.trim(value)) not in ["0", "false", "off", "no"]
    end
  end

  defp decode(output) when is_binary(output) do
    case find_json_object(output) do
      nil ->
        {:error, {:eligibility_unparseable, String.slice(output, 0, 800)}}

      %{"status" => _} = result ->
        {:ok, result}

      other ->
        {:error, {:eligibility_invalid_result, other}}
    end
  end

  defp find_json_object(output) do
    case :binary.matches(output, "{") do
      [] ->
        nil

      matches ->
        output
        |> find_last_decodable_json(Enum.map(matches, fn {position, _length} -> position end))
    end
  end

  defp find_last_decodable_json(_output, []), do: nil

  defp find_last_decodable_json(output, [position | rest]) do
    candidate = binary_part(output, position, byte_size(output) - position)

    case Jason.decode(candidate) do
      {:ok, decoded} -> decoded
      _ -> find_last_decodable_json(output, rest)
    end
  end
end

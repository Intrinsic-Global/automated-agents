defmodule SymphonyElixir.Linear.Adapter do
  @moduledoc """
  Linear-backed tracker adapter.
  """

  @behaviour SymphonyElixir.Tracker

  alias SymphonyElixir.Linear.{AgentTool, Client}
  alias SymphonyElixir.Tracker.Issue

  @spec validate_config(map()) :: :ok | {:error, term()}
  def validate_config(tracker_settings) do
    with :ok <- validate_string(tracker_settings.endpoint, :invalid_linear_endpoint),
         :ok <- validate_string(tracker_settings.api_key, :missing_linear_api_token),
         :ok <- validate_project_scope(tracker_settings) do
      validate_assignee(tracker_settings)
    end
  end

  @spec fetch_issues_by_states([String.t()]) :: {:ok, [Issue.t()]} | {:error, term()}
  def fetch_issues_by_states(states), do: client_module().fetch_issues_by_states(states)

  @spec fetch_issues_by_ids([String.t()]) :: {:ok, [Issue.t()]} | {:error, term()}
  def fetch_issues_by_ids(issue_ids), do: client_module().fetch_issues_by_ids(issue_ids)

  @spec agent_tool_specs() :: [map()]
  def agent_tool_specs, do: AgentTool.tool_specs()

  @spec execute_agent_tool(String.t(), term(), keyword()) :: map()
  def execute_agent_tool(tool, arguments, opts) do
    AgentTool.execute(tool, arguments, opts)
  end

  @spec secret_environment_names(map()) :: [String.t()]
  def secret_environment_names(tracker_settings), do: tracker_settings.secret_environment_names

  defp client_module do
    Application.get_env(:symphony_elixir, :linear_client_module, Client)
  end

  defp validate_string(value, error) do
    if present_string?(value), do: :ok, else: {:error, error}
  end

  defp validate_project_scope(tracker_settings) do
    cond do
      project_scopes_conflict?(tracker_settings) ->
        {:error, :conflicting_linear_project_scope}

      project_scope_configured?(tracker_settings) ->
        :ok

      true ->
        {:error, :missing_linear_project_slug}
    end
  end

  defp project_scopes_conflict?(tracker_settings) do
    is_list(tracker_settings.project_slugs) and tracker_settings.project_slugs != [] and
      is_list(tracker_settings.excluded_project_slugs) and tracker_settings.excluded_project_slugs != []
  end

  defp validate_assignee(tracker_settings) do
    if is_nil(tracker_settings.assignee) or present_string?(tracker_settings.assignee) do
      :ok
    else
      {:error, :invalid_linear_assignee}
    end
  end

  defp present_string?(value) when is_binary(value), do: String.trim(value) != ""
  defp present_string?(_value), do: false

  defp project_scope_configured?(tracker_settings) do
    present_string?(tracker_settings.project_slug) or
      nonempty_string_list?(tracker_settings.project_slugs) or
      nonempty_string_list?(tracker_settings.excluded_project_slugs) or
      nonempty_string_list?(tracker_settings.issue_identifiers) or
      nonempty_string_list?(tracker_settings.excluded_issue_identifiers)
  end

  defp nonempty_string_list?(values) when is_list(values) and values != [] do
    Enum.all?(values, &present_string?/1)
  end

  defp nonempty_string_list?(_values), do: false
end

System.put_env("SYMPHONY_ELIGIBILITY_ENFORCE", "0")
ExUnit.start()
Code.require_file("support/snapshot_support.exs", __DIR__)
Code.require_file("support/test_support.exs", __DIR__)

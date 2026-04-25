LLMs make a lot of errors when writing Python code for the Unreal Python API, especially where the API changes between versions.
Now you can ask your agent to verify if a code is correct by running it in an open Unreal session. The harness returns the log output for the script,
so it can also iterate until the script works.
When working on a larger script, the agent can also run short oneliners inside UE to make sure the API calls are correct for your UE version.


Installing

Add to [user]/.codex/skills/ or whatever is the right place for your agent. So you have e.g. C:\Users\eetu\.codex\skills\ue-python-harness\SKILL.md

You also need to enable remote Python execution in your Unreal Editor settings (Project Settings -> Plugins -> Python -> Enable Remote Execution)



Hint: Also pointing your agent to the Unreal source code will give you a double whammy of goodness.

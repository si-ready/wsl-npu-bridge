@{
    # PSScriptAnalyzer settings for the host scripts.
    #
    # PSAvoidUsingWriteHost is excluded DELIBERATELY: these are
    # interactive installer/service scripts whose console output IS
    # the user interface. Write-Output would pollute the pipeline
    # (several scripts are invoked with & from each other and their
    # success is judged by what they print); Write-Information is
    # invisible by default. Every other rule, including all other
    # warnings, stays enforced in CI.
    ExcludeRules = @('PSAvoidUsingWriteHost')
}

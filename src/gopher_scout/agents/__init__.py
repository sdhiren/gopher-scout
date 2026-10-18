"""The agents. Each owns exactly one stage of the pipeline.

==============  =================================================  =========
Agent           Responsibility                                      Uses LLM
==============  =================================================  =========
Scout           find candidate Go CLI repos on GitHub               no
Profiler        clone + static analysis + contribution policy       no
Analyst         propose evidence-backed improvement ideas           yes
Critic          verify, de-duplicate, score; gate what you see      yes
Implementer     implement an approved idea on a local branch        yes
Reviewer        build/vet/test in a sandbox + diff review           partly
==============  =================================================  =========
"""

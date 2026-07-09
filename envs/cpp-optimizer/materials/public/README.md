# Weighted Job Scheduling

Write a C++17 program that reads a set of jobs, each with a start time,
end time, and weight, and selects a subset of **non-overlapping** jobs that
maximizes total weight.

## Input format (stdin)

```
n
s_1 e_1 w_1
s_2 e_2 w_2
...
s_n e_n w_n
```

- `1 <= n <= 20000`
- `0 <= s_i < e_i <= 1_000_000_000`
- `1 <= w_i <= 1_000_000`
- Two jobs `i` and `j` overlap if their `[s, e)` intervals intersect (touching
  endpoints, i.e. `e_i == s_j`, do **not** count as overlapping).

## Output format (stdout)

A single integer: the maximum total weight achievable by any subset of
mutually non-overlapping jobs.

## Example

Input:
```
4
1 3 5
2 5 6
4 6 5
6 7 4
```

Output:
```
14
```

(Pick jobs 1 `[1,3)` w=5, 3 `[4,6)` w=5, 4 `[6,7)` w=4 → total 14. Job 2
overlaps both job 1 and job 3.)

## Submission

Write your final solution to `solution.cpp` in the current working
directory. It will be compiled with:

```
g++ -std=c++17 -O2 -o solution solution.cpp
```

and run once per test case with a 5 second time limit.

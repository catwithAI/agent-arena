#!/usr/bin/env python3
"""iterative-tuning 提交文件。

打印一行 12 个数字（每个 ∈ [0, 10]），作为提交的参数。评分时会用这组参数调
黑盒目标函数打分（越大越好）。

下面是朴素基线（全 5.0）——它只能拿中等分。请用 tools/tester.py 反复评分、迭代
调参（坐标爬山 / 随机搜索都行），逼近更高分再提交。
"""


def main() -> None:
    params = [5.0] * 12
    print(" ".join(str(p) for p in params))


if __name__ == "__main__":
    main()

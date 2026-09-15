"""tests 必须作为常规包存在:本机 site-packages 里有第三方顶层 `tests` 包,
常规包优先级高于命名空间包,缺本文件时本地 fixtures 会被遮蔽(import 失败)。"""

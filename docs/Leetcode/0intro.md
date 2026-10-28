---
sidebar_position: 1
---

# Leetcode
根据题目要求输出的不同，例如两数之和，可能使用hash更好或者使用排序双指针更好

根据时空复杂度判断需要怎么做
## 一、两数之和
1. 暴力枚举 o(n2)
2. 哈希
暴力枚举的原因是找到与当前值相加为target的数的时候太难了，仍需要一次遍历。那么第一层就边遍历边哈希
有个细节，哈希是边遍历边生成的，也就是说当前能查找到的只有遍历过的，因此如果从哈希表中找到，那么当前下标是目标值中下标更大的，因此返回&#123;hash.get(target-arr[i]), i&#125;

注意和三数之和相比，这题假定每个有唯一答案，也就是说不会重复
```python
class Solution:
    def twoSum(self, nums: list[int], target: int) -> list[int]:
        hash_map = {}
        for i, num in enumerate(nums):
            les = target - num
            if les in hash_map:
                return [hash_map[les],i]
            hash_map[num] = i
        return []
```
## 一百二十八、最长连续序列
其实和两数之和差不多，都是有一个暴力解法，两层遍历，但是可以通过哈希快速查找的特性，把数组放到哈希表里面，就可以直接根据当前数快速判断后面是否有连续（还有一点是需要判断这是第一个数，判断没有num-1）
而且其实这里没有要求输出下标，只需要使用hashset

不能排序（题目要求 O(n)）。
我需要频繁判断某个元素是否存在 → 想到 HashSet。
如何避免重复遍历？ → 只从连续序列的起点开始（即 num - 1 不存在）。
每个元素最多被扩展访问一次 → 总时间复杂度为 O(n)。
```python
# class Solution:
#     def groupAnagrams(self, strs: list[str]) -> list[list[str]]:
#         hash_map = {}
#         for stri in strs:
#             sortedstr = sorted(stri)
#             if sortedstr in hash_map:
#                 hash_map[sortedstr] += stri
#             hash_map[sortedstr] = stri
        
#         return [[""]]
class Solution:
    def groupAnagrams(self, strs: list[str]) -> list[list[str]]:
        hash_map = {}
        for stri in strs:
            sortedstr = sorted(stri)
            key = "".join(sortedstr)
            if key in hash_map:
                hash_map[key].append(stri) #+= [stri]
            else:
                hash_map[key] = [stri]
        
        return list(hash_map.values())
```
## 四十九、字母异位词分组
其实就是几个乱序的东西分类
最先想到的是他们有序就可以判断出哪些是一样的
那么也就是对于每个字符都排序，循环里面一个排序nklogk
但是排序之后的数字怎么归类呢，其实现在问题变成了
如何把拥有相同 key 的东西放一起？哈希

什么时候想到哈希
- 分类（重点是构造一个特征作为key，就像这题可以排序作为特征或者数组计数作为特征）
- 统计次数
- 快速查找
- 去重

① 什么样的字符串算一样？ ↓ 排序以后一样 
② 能不能把它变成同一种表示？ ↓ 可以，排序后的字符串 
③ 现在问题变成什么？ ↓ 把拥有同一个表示的字符串放一起 
④ 什么数据结构最适合"按键归类"？ ↓ HashMap Map&lt;排序后的字符串,List&lt;原字符串>>
很多字符串题（异位词、同构字符串、频次相同等）都会用到这种模式。一旦你发现可以为每个对象构造一个唯一的"特征"，接下来就应该联想到：用这个特征作为 HashMap 的 key，把具有相同特征的对象归到同一个列表里。
```python
class Solution:
    def longestConsecutive(self, nums: list[int]) -> int:
        hash_map = {} #set更好
        for num in nums:
            hash_map[num] = True
        
        max = 0
        count = 0
        for num in hash_map: #写nums会超时，原始数组有重复 → 超时
            now = num
            count = 1
            if (now-1) not in hash_map: #重要
                while (now+1) in hash_map:
                    now +=1
                    count+=1
                if count > max:
                    max = count
        return max
```
            
## 二百八十三、移动零(x)
是否需要维护两个位置
是不是需要不断缩小一个区间
是不是需要维护一个连续区间
是不是快慢不同？

最直接的方法是新建一个数组，把非零全部移动过去，但是要求原地
那么我们发现只是把非零往前移动，使用两个指针，一个是有序区，一个未排序区

```python
# class Solution:
#     def moveZeroes(self, nums: list[int]) -> None:
#         """
#         Do not return anything, modify nums in-place instead.
#         """
#         if len(nums) == 1:
#             return
#         p = 0
#         q = 1
#         while q < len(nums):
#             while(nums[q]==0) and (q < len(nums)-1):
#                 q+=1
#             if nums[p] == 0 and nums[q] != 0:
#                 nums[p] = nums[q]
#                 nums[q] = 0
#                 p+=1
            
# class Solution:
#     def moveZeroes(self, nums: list[int]) -> None:
#         """
#         Do not return anything, modify nums in-place instead.
#         """
#         slow = 0
#         for fast in range(len(nums)):
#             if nums[fast] != 0:
#                 # 交换slow和fast位置
#                 nums[slow], nums[fast] = nums[fast], nums[slow]
#                 slow += 1
class Solution:
    def moveZeroes(self, nums: list[int]) -> None:
        """
        Do not return anything, modify nums in-place instead.
        """
        p = 0
        q = 1
        n = len(nums)
        while q < n:
            # p位置不是0，p先往前走，找0
            if nums[p] != 0:
                p += 1
                q += 1
            else:
                # p是0，看q
                if nums[q] != 0:
                    # 交换
                    nums[p], nums[q] = nums[q], nums[p]
                    p += 1
                    q += 1
                else:
                    # q也是0，q继续往后
                    q += 1
```


## 十一、盛最多水的容器
首先想到暴力解，双循环
感觉这道题最主要的在于面积公式，
```python
class Solution:
    def maxArea(self, height: list[int]) -> int:
        left = 0
        right = len(height)-1
        max = min(height[left], height[right]) * (right - left)
        while(left < right):
            if height[left] > height[right]:
                right-=1
                now =  min(height[left], height[right]) * (right - left)
            else:
                left+=1
                now =  min(height[left], height[right]) * (right - left)
            if now > max:
                max = now

        return max
```
        

## 十五、三数之和
先想暴力解，三重循环，但是要求不重复，如果全0就很难暴力解
a+b+c=0，其实就是a+b=-c
可以直接使用两数之和哈希方法，但是这个题目要求不重复，因此这样最后需要消除重复

双指针
- 两个位置共同决定答案
- 数组有序，且可以排除一半情况
- 需要维护一个连续区间（滑动窗口）
快慢指针
滑动窗口
左右指针
```python
# class Solution:
#     def threeSum(self, nums: list[int]) -> list[list[int]]:
#         p = 0
#         q = 1
#         for p in range(0, len(nums)):
#             for q in range(p+1, len(nums)):
#                 if nums[p]+nums[q] == 
# class Solution:
#     def threeSum(self, nums: list[int]) -> list[list[int]]:
#         res = []
#         nums.sort()
#         if(nums[0] > 0) :
#             return []
#         for i in range(len(nums)):
#             left = i+1
#             right = len(nums)-1
#             if nums[left] + nums[right] + nums[i] > 0:
#                 right -=1
#             elif nums[left] + nums[right]  +nums[i] <0:
#                 left+=1
#             else:
#                 res.append([nums[i], nums[left], nums[right]])
            
class Solution:
    def threeSum(self, nums: list[int]) -> list[list[int]]:
        res = []
        nums.sort()
        if(nums[0] > 0) :
            return []
        for i in range(len(nums)):
            if i > 0 and nums[i] == nums[i-1]: #去重
                continue
            left = i+1
            right = len(nums)-1
            while left < right: # 不然数组outofrange
                if nums[left] + nums[right] + nums[i] > 0:
                    right -=1
                elif nums[left] + nums[right]  +nums[i] <0:
                    left+=1
                else:
                    res.append([nums[i], nums[left], nums[right]])
 
                    while left < right and nums[left] == nums[left+1]: #去重
                        left += 1
                    while left < right and nums[right] == nums[right-1]:
                        right -= 1
                    right -=1 #先去重再移动
                    left+=1
        return res
```
哈希的核心是快速判断有没有，双指针的核心是利用顺序，让搜索方向确定。
## 四十二、接雨水
不能从左到右双指针，右边最大值不是最近那根柱子！而要找右边全局最高
## 三、无重复字符的最长子串
很好想，双指针窗口，窗口内使用哈希快速判断是否有重复，只需o(n)
但是双指针的代码小心不要写成暴力
```python
# class Solution:
#     def lengthOfLongestSubstring(self, s: str) -> int:
#         hash_map = set()
#         p = 0
#         q = 0
#         res = 0
#         max = 0
#         for i in range(len(s)):
#             hash_map.discard(s[i])
#             for j in range(i, len(s)):
#                 res = 0
#                 if s[j] not in hash_map:
#                     hash_map.add(s[j])
#                     res+=1
#                 else:
#                     if res>max:
#                         max = res
#                     res = 0
#                     break
            
#         return max
class Solution:
    def lengthOfLongestSubstring(self, s: str) -> int:
        hash_map = set()
        p = 0
        q = 0
        max1 = 0
        for q in range(len(s)):
            while s[q] in hash_map:
                hash_map.discard(s[p]) #顺序不能反
                p+=1
                
            hash_map.add(s[q])
            max1 = max(max1, q - p +1)
        return max1
```
## 四百三十八、找到字符串中所有字母异位词
还是双指针窗口，主要是判断里面是否相同，可以每个计数判断
```python
# class Solution:
#     def findAnagrams(self, s: str, p: str) -> List[int]:
#         hash_map = {}
#         hashmap = {}
#         n = len(p)
#         res = []
#         for i in range(len(p)):
#             if p[i] in hash_map:
#                 hash_map[p[i]] += 1
#             else:
#                 hash_map[p[i]] = 1
#             if s[i] in hashmap:
#                 hashmap[s[i]] += 1
#             else:
#                 hashmap[s[i]] = 1
#         p = 0
#         q = n-1

#         while q < len(s):
#             if hash_map == hashmap:
#                 res.append(p)
#             p+=1
#             q+=1
#             if q < len(s):
#                 if s[q] in hashmap:
#                     hashmap[s[q]] +=1
#                 else:
#                     hashmap[s[q]] = 1
#                 hashmap[s[p]] -=1
        
#         return res
class Solution:
    def findAnagrams(self, s: str, p: str) -> List[int]:
        if len(p) > len(s):
            return []
        hash_map = {}
        hashmap = {}
        n = len(p)
        res = []
        for i in range(len(p)):
            if p[i] in hash_map:
                hash_map[p[i]] += 1
            else:
                hash_map[p[i]] = 1
            if s[i] in hashmap:
                hashmap[s[i]] += 1
            else:
                hashmap[s[i]] = 1
        p = 0
        q = n-1

        while q < len(s):
            if hash_map == hashmap:
                res.append(p)
            p+=1
            q+=1
            if q < len(s):
                if s[q] in hashmap:
                    hashmap[s[q]] +=1
                else:
                    hashmap[s[q]] = 1
                hashmap[s[p-1]] -=1
                if hashmap[s[p-1]] == 0:  # 修改3
                    del hashmap[s[p-1]]
        
        return res
```

            

            
## 五百六十、和为k的子数组
由于是连续的，直接哈希map不太好找，区间可能想到双指针，但是直接枚举需要n2
这时候就需要前缀和，前缀和经常和双指针，连续区间结合
想到前缀和就很好写了，子数组就是两个前缀和相减，其实现在基本上就转换成两数之差了，使用hash快速找
前缀和不是和原数组一样线性的，只表示是否有这么个前缀和，出现过几次
## 二百三十九、滑动窗口最大值
窗口每次只移动一个位置，因此没有必要重新计算整个窗口；我们用一个"单调递减队列"维护所有"未来仍有可能成为最大值"的元素。队头始终是当前窗口最大值，而那些比新元素小且更靠左的元素，由于以后不可能再成为最大值，可以立即删除。
使用一个deque（双端队列），队列里存的是下标，不是数字。
```text
for(int i=0;i<n;i++){
    ① 删除窗口外元素
    ② 维护单调递减(比nums[i]小的都删掉）
    ③ 加入当前元素（nums[i]加入队列)

    如果窗口形成
        输出答案
}
```
## 七十六、最小覆盖子串
右指针不断扩大寻找可行解，左指针不断收缩寻找最优解。
## 五十三、最大子数组和
1. 其实可以使用之前的前缀和的思路，转换为前缀和数组，然后就是找到最大的差，遍历的时候使用一个变量记录已经遍历过的最小值并不断更新，然后不断只用当前值减去最小值从而找到最大数组和
2. 其实这道题标准解法是动态规划
f(i)=max&#123;f(i−1)+nums[i],nums[i]&#125;
数组题的本质经常是：
暴力枚举 → 发现重复计算 → 利用数据结构或者规律减少计算。
暂时无法在飞书文档外展示此内容
## 五十六、合并区间
## 一百八十九、轮转数组
## 二百三十八、除了自身以外数组的乘积
## 四十一、缺失的第一个正数
## 一百六十、相交链表
让两个指针走过相同的路程，那么它们一定会在交点相遇。
## 二百零六、反转链表
头插法
迭代反转
```java
class Solution {
    public ListNode reverseList(ListNode head) {
        ListNode prev = null;
        ListNode curr = head;
        while (curr != null) {
            ListNode next = curr.next;
            curr.next = prev;
            prev = curr;
            curr = next;
        }
        return prev;
    }
}
```
递归！！！
## 二百三十四、回文链表
1. 如果允许额外空间，可以直接复制到数组里面处理
2. 如果不允许，使用快慢指针找到中点，反转后半部分比较
3. 递归（比较难想）
## 一百四十一、环形链表
很简单，类似相交
1. 哈希
2. 快慢指针
## 一百四十二、环形链表2
需要找到入口，哈希可以直接判断入口
[图片]
fast走过a+n(b+c)+b=a+(n+1)b+nc。
slow走过a+b
fast=2slow
a+(n+1)b+nc=2(a+b)⟹a=c+(n−1)(b+c)
可以看出从相遇点到入环点的距离加上 n−1 圈的环长，恰好等于从链表头部到入环点的距离。
因此，当发现 slow 与 fast 相遇时，我们再额外使用一个指针 ptr。起始，它指向链表头部；随后，它和 slow 每次向后移动一个位置。最终，它们会在入环点相遇。

## 二十一、合并两个有序链表
## 二、两数相加
## 十九、删除链表倒数第n个结点
其实遍历两遍和双指针遍历一遍复杂度一样的
## 二十四、两两交换链表中的节点
把握好迭代指针的顺序即可，可以记一下
递归也可以，背下来得了
## 二十五、k个一组翻转链表
上一题的扩展，只能用迭代了
也可以看作反转链表的扩展
## 一百三十八、随机链表的复制
这道题最难的就是去找到问题在哪，理解问题。其实就是你拷贝节点的时候要让新结点的指针指向新结点而不是旧结点
由于 random 可能指向链表中的任意节点，因此在复制当前节点时，无法直接知道它应该指向哪个新节点。我的做法是使用一个 HashMap 保存 原节点 → 新节点 的映射。第一次遍历只创建所有新节点并建立映射；第二次遍历根据映射设置每个新节点的 next 和 random 指针，最后返回新链表头节点即可。
```cpp
class Solution {
public:
    Node *copyRandomList(Node *head) {        
        unordered_map<Node *, Node *> mp;
        for (auto p = head; p; p = p->next) {
            mp[p] = new Node(p->val);            
        }
        for (auto p = head; p; p = p->next) {
            mp[p]->next = mp[p->next];
            mp[p]->random = mp[p->random];
        }  
        return mp[head];
    }
};
```
## 一百十四八、排序链表
首先可以想到没有空间时间要求的话，使用哈希建立一个数组映射表，然后排序数组，再根据下标对应关系进行重组，其实甚至不需要哈希表，数组本身就可以表示映射，但是空间复杂度on
但是归并排序
自顶向下 ologn空间
```text
sort(head):

    if 一个节点:
        return head

    找中点

    左边 = sort(左链表)

    右边 = sort(右链表)

    return merge(左边,右边)
```

自底向上
```text
size = 1

while(size < n){

    cur = head

    while(cur != null){

        left = 当前size长度
        right = 下一个size长度

        合并left和right

        cur移动到下一组
    }

    size *= 2
}
```
## 二十三、合并k个升序链表
暂时无法在飞书文档外展示此内容
## 一百十四六、LRU缓存
这个就记住吧，LRU = 哈希表 + 双向链表
HashMap负责定位，双向链表负责排序。

## 九十四、二叉树的中序遍历
```text
function inorder(root):

    if root == null:
        return


    inorder(root.left)     // 遍历左子树


    visit(root)            // 访问当前节点


    inorder(root.right)    // 遍历右子树
function inorder(root):

    stack = empty

    current = root


    while current != null OR stack不为空:


        // 先把左边节点全部压入栈
        while current != null:

            stack.push(current)

            current = current.left



        // 左边走到底，访问栈顶节点

        current = stack.pop()

        visit(current)



        // 转向右子树

        current = current.right
```
## 一百零四、二叉树的深度
```text
function maxDepth(root):

    if root == null:
        return 0


    leftDepth = maxDepth(root.left)

    rightDepth = maxDepth(root.right)


    return max(leftDepth, rightDepth) + 1
```
## 二百二十六、翻转二叉树
```text
function invertTree(root):

    if root == null:
        return null


    swap(root.left, root.right)


    invertTree(root.left)

    invertTree(root.right)


    return root
```
其实就是考的遍历

## 一百零一、对称二叉树
还是递归，树遍历
```cpp
class Solution {
public:
    bool check(TreeNode *p, TreeNode *q) {
        if (!p && !q) return true;
        if (!p || !q) return false;
        return p->val == q->val && check(p->left, q->right) && check(p->right, q->left);
    }

    bool isSymmetric(TreeNode* root) {
        return check(root->left, root->right);
    }
};
```
## 五百四十三、二叉树的直径
这个题最主要的是搞懂怎么算直径
对于任意一个节点，它可能成为最长路径的拐点。
例如
```text
      1
     / \
    2   3
```
   / \
  4   5
最长路径为
4 -> 2 -> 1 -> 3
经过节点 1。
而对于某个节点 x：
- 左边最长向下路径长度 = 左子树高度
- 右边最长向下路径长度 = 右子树高度
那么
经过 x 的最长路径
= 左高度 + 右高度
因为题目要求的是边数。
因此：
- 遍历每一个节点
- 计算它左右子树高度
- 更新全局最大值
最后返回最大值即可。
```text
ans = 0

dfs(node):

    if node为空
        return 0

    left = dfs(node.left)
    right = dfs(node.right)

    ans = max(ans, left + right)

    return max(left, right) + 1
```

调用 dfs(root)

返回 ans
可以总结
二叉树的直径 = 所有节点中，左右子树高度之和的最大值。
所以其实就是求二叉树左右子树高度和最大值

## 一百零八、将有序数组转换为二叉树
由于已经有序，平衡二叉树可以很简单的构造
```text
build(left,right):

    if left > right
        return None

    mid = (left + right) // 2
    # 当然，这里选择根结点左边或者右边作为根结点

    root = TreeNode(nums[mid])

    root.left = build(left, mid-1)
    root.right = build(mid+1, right)

    return root
```
## 九十八、验证二叉搜索树
很多人第一次都会想到：
只判断每个节点：左孩子 &lt; 根 &lt; 右孩子。
这是错误的。
需要要求左边全部都小于根结点
例如：
```text
      5
     / \
    1   7
       / \
      4   8
```
1. 递归
```text
dfs(node, low, high):

    if node为空
        return True

    if node.val <= low
        return False

    if node.val >= high
        return False

    return
        dfs(node.left, low, node.val)
        &&
        dfs(node.right, node.val, high)
```
2. 中序遍历 bst中旭一定递增
```python
class Solution:
    def isValidBST(self, root):
        prev = None

        def inorder(node):
            nonlocal prev

            if not node:
                return True

            if not inorder(node.left):
                return False

            if prev and node.val <= prev.val:
                return False

            prev = node

            return inorder(node.right)

        return inorder(root)
```
- 遍历：规定访问节点的顺序（前序、中序、后序、层序）。
- 递归：一种实现方法。这里的递归不是遍历，只是树天然适合递归
## 二百三十、二叉搜索树中第k小的元素
和上一题一样，还是使用
BST 的中序遍历结果是递增序列。
```text
count = 0
ans = None

dfs(node):

    if node为空:
        return

    dfs(node.left)

    count += 1

    if count == k:
        ans = node.val
        return

    dfs(node.right)
```
可以简单理解成：
方法二：记录每个节点的子树大小
普通中序遍历的问题：
不知道第 k 小的位置在哪里，只能一个个数。
所以给每个节点增加一个信息：
size = 这个节点下面一共有多少个节点
例如：
```text
        5
      /   \
     3     7
    / \
```
   2   4
记录：
5 的左子树 size = 3
3 的左子树 size = 1
查找第 k 小时：
看当前节点：
情况1：左子树节点数 = k-1
说明：
左边刚好有 k-1 个更小的

当前节点就是第 k 小
返回。

---
情况2：左子树节点数 > k-1
说明：
第 k 小还在左边
去左子树找。

---
情况3：左子树节点数 &lt; k-1
说明：
左边和当前节点都不够 k 个

答案在右边
去右子树找，同时：
k -= 左子树数量 + 1
因为左边和当前节点已经跳过了。

---
所以方法二本质：
给 BST 增加“每边有多少节点”的信息，让查找像二分一样，不需要遍历。
复杂度：
- 普通 BST：O(树高)
- 平衡 BST：O(log n)

---
方法三：平衡二叉搜索树 + size
方法二有一个问题：
如果 BST 长歪：
1
 \
  2
   \
```text
    3
     \
      4
```
高度变成：
O(n)
那么查找还是慢。
所以方法三：
在方法二基础上，再保证树不要长歪。
比如使用：
- AVL树
- 红黑树
它们会通过旋转自动调整结构：
原来：
1
 \
  2
   \
    3
调整成：
    2
   / \
  1   3
让高度保持：
O(log n)
同时继续维护：
size
这样：
- 插入：O(log n)
- 删除：O(log n)
- 查第 k 小：O(log n)

---
一句话总结：
- 方法2：给普通 BST 加 size，知道每边多少个节点，快速定位第 k 小。
- 方法3：再让 BST 保持平衡，避免退化，使所有操作稳定达到 O(log n)。
你可以把方法3理解成：
方法2解决“怎么快速找”，方法3解决“怎么保证树一直快”。
## 一百九十九、二叉树的右视图
1. 最简单的是层序遍历（广度优先），直接每层的最后一个元素
2. 深度优先遍历的话，在搜索过程中，我们总是先访问右子树。那么对于每一层来说，我们在这层见到的第一个结点一定是最右边的结点。
遍历分为层序和深度优先，前序中序后序是根据根结点访问顺序分的，其实可以自定义遍历的顺序，具体的遍历可以通过栈或者递归实现。层序也可以递归或者队列
## 一百一十四、二叉树展开为链表
1. 说了展开为前序遍历，那么就直接前序遍历，然后一个个放到暂存数组里，然后再组合
2. 之前存放是因为前序遍历先放的话会丢失结点和指针？xx
3. 可以后续
## 一百零五、从前序和中序遍历序列构造二叉树
不多说，背吧
```python
class Solution:
    def buildTree(self, preorder, inorder):

        if not preorder:
            return None

        # 前序第一个是根
        root_val = preorder[0]
        root = TreeNode(root_val)

        # 找根在中序中的位置
        mid = inorder.index(root_val)

        # 左子树
        root.left = self.buildTree(
            preorder[1:mid+1],
            inorder[:mid]
        )

        # 右子树
        root.right = self.buildTree(
            preorder[mid+1:],
            inorder[mid+1:]
        )

        return root
```
## 四百三十七、路径总和3
这种连续和是不是有点类似数组前缀和的思路
1. 暴力
对于每个节点：
以它作为起点，找下面有多少路径满足 targetSum
再递归处理左子树
再递归处理右子树
2. 前缀和+回溯
初始化:
```text
    prefixMap = {0: 1}
    ans = 0


DFS(node, currentSum):

    如果 node 为空:
        返回 0


    currentSum += node.val


    # 查找是否存在满足条件的路径
    ans += prefixMap[currentSum - targetSum]


    # 记录当前前缀和
    prefixMap[currentSum] += 1


    # 继续向下搜索
    DFS(node.left, currentSum)

    DFS(node.right, currentSum)


    # 回溯，删除当前路径状态
    prefixMap[currentSum] -= 1
```



主函数:

```text
    DFS(root, 0)

    返回 ans
```
## 二百三十六、二叉树的最近公共祖先
对于一个节点 root：
我们问：
p 和 q 是否分别在我的左子树、右子树？
有三种情况：
情况1：p、q 分别在左右两边
例如：
```text
        3
       / \
      5   1
```
如果：
p在左边
q在右边
那么：
当前节点3就是最近公共祖先

---
情况2：两个节点都在左边
例如：
```text
        3
       /
      5
     /
    6
```
那么：
答案一定在左子树
继续递归：
去左边找

---
情况3：两个节点都在右边
同理：
去右子树找

```text
---

DFS(node):

    如果 node为空:
        返回 None


    如果 node == p 或 node == q:
        返回 node


    left = DFS(node.left)

    right = DFS(node.right)


    如果 left 不为空 且 right 不为空:
        说明p、q分别在两边

        当前node就是最近公共祖先
        返回 node


    如果 left 不为空:
        返回 left


    如果 right 不为空:
        返回 right


    返回 None
```
## 一百二十四、二叉树中的最大路径和

## 二百、岛屿数量

初始化岛屿数量 = 0
遍历网格每一个单元格(i,j):
```text
    若当前是陆地"1":
        岛屿数量 += 1
        清除该岛屿所有连通陆地(DFS/BFS):
            遇到陆地就改成水"0"，向上下左右延伸
```
全部遍历完成，返回岛屿数量
## 九百九十四、腐烂的橘子
```text
function orangesRotting(grid):
    m = 行数
    n = 列数
    queue = 空队列
    fresh = 0
    time = 0
    dirs = [(-1,0),(1,0),(0,-1),(0,1)] 上下左右四方向

    // 第一步：初始化队列、统计新鲜橘子
    for i from 0 to m-1:
        for j from 0 to n-1:
            if grid[i][j] == 2:
                queue.append( (i,j) )
            else if grid[i][j] == 1:
                fresh = fresh + 1

    // 没有新鲜橘子，无需等待
    if fresh == 0:
        return 0

    // 多源BFS逐层扩散
    while queue 不为空 and fresh > 0:
        size = 当前队列长度  // 当前这一层所有腐烂橘子
        for k from 0 to size - 1:
            x,y = 队首出队
            for (dx, dy) in dirs:
                nx = x + dx
                ny = y + dy
                // 坐标合法 且 是新鲜橘子
                if 0<=nx<m and 0<=ny<n and grid[nx][ny] == 1:
                    grid[nx][ny] = 2   // 腐烂
                    fresh -= 1
                    queue.append( (nx, ny) )
        time += 1  // 一层扩散完成，时间+1

    // 还有剩余新鲜橘子，无法全部腐烂
    if fresh > 0:
        return -1
    else:
        return time
```
网格图的特殊表示
## 二百零七、课程表
拓扑排序
核心问题：有向图判环
[a, b]：学a必须先学b → 边：b → a 有环 → 返回False；无环 → 返回True

---
方案一：Kahn BFS 拓扑排序
思路
1. 构建邻接表存图；构建入度数组记录每个节点有多少前置课程。
2. 把所有入度等于0的节点（没有先修课）加入队列。
3. 循环取出队首节点，计数+1；遍历它所有后继节点，后继的入度减一；如果某个后继入度变成0，就送入队列。
4. 最后比较：被取出的节点数量 == 总课程数？
  - 相等：全部课程都能上完，无环 → True
  - 不等：剩下节点形成环 → False
伪代码
```text
函数 canFinish(numCourses, prerequisites):
    // 1.初始化图、入度数组
    graph = 长度为numCourses的空列表数组
    in_degree = 长度为numCourses、初始全0的数组

    for each [a, b] in prerequisites:
        graph[b].append(a)     // 边 b → a
        in_degree[a] = in_degree[a] + 1

    // 2.队列保存入度为0的节点
    queue = 空队列
    for i from 0 to numCourses‑1:
        if in_degree[i] == 0:
            queue.push(i)
    
    count = 0

    // 3.BFS遍历
    while queue 不为空:
        cur = queue.pop_front()
        count = count + 1
        for each nxt in graph[cur]:
            in_degree[nxt] = in_degree[nxt] - 1
            if in_degree[nxt] == 0:
                queue.push(nxt)
    
    // 4.判断
    return count == numCourses

---
```
方案二：DFS 三色标记判环
思路
给每个节点标记3种访问状态：
- 0：未访问
- 1：访问中（当前递归栈里），如果再次碰到状态为1的节点 → 发现环
- 2：已经访问完毕，这条路径没问题
1. 先建好邻接表。
2. 逐个遍历每一个课程节点，如果没访问过就启动DFS。
3. DFS流程：
  - 如果节点标记=1：遇到环 → 返回False
  - 如果节点标记=2：安全，返回True
  - 标记当前节点=1（进入递归栈）
  - 递归访问所有后继节点，只要一个后继返回False，则整体False
  - 全部后继遍历完成无环，标记当前节点=2，返回True
4. 只要任意一次DFS检出环，答案就是False；全部正常则True。
伪代码
```text
函数 canFinish(numCourses, prerequisites):
    graph = 长度numCourses的空列表数组
    for each [a, b] in prerequisites:
        graph[b].append(a)
    
    // 0:未访问, 1:访问中, 2:已完成
    visited = [0] * numCourses

    // 深度优先搜索
    函数 dfs(node):
        if visited[node] == 1:
            return False       // 找到环
        if visited[node] == 2:
            return True
        
        visited[node] = 1      // 标记为访问中
        for each nxt in graph[node]:
            if dfs(nxt) == False:
                return False
        visited[node] = 2      // 回溯，标记完成
        return True
    
    // 遍历所有起点
    for i from 0 to numCourses‑1:
        if dfs(i) == False:
            return False
    
    return True

---
```
两种方法对比
暂时无法在飞书文档外展示此内容
## 二百零八、实现前缀树
没什么难点
```text
// Trie树节点
结构体 TrieNode:
    children = 大小26的数组，初始全部null
    isEnd = false

类 Trie:
    // 根节点
    root = TrieNode()

    函数 insert(word):
        cur = root
        for char c in word:
            index = c - 'a'
            if cur.children[index] == null:
                cur.children[index] = TrieNode()
            cur = cur.children[index]
        cur.isEnd = true   // 标记单词结尾

    函数 search(word) -> bool:
        cur = root
        for char c in word:
            index = c - 'a'
            if cur.children[index] == null:
                return false
            cur = cur.children[index]
        // 走到末尾，必须是单词终点
        return cur.isEnd

    函数 startsWith(prefix) -> bool:
        cur = root
        for char c in prefix:
            index = c - 'a'
            if cur.children[index] == null:
                return false
            cur = cur.children[index]
        // 只要前缀路径存在即可，不用判断结尾标记
        return true
```
## 四十六、全排列
主要就是回溯
结果集 result = 空列表
```text
used = 布尔数组，标记元素是否被选取，初始全 false

函数 backtrack(path):
    if path 的长度 == nums长度:
        把 path 的副本加入 result
        return
    
    for i from 0 to len(nums)-1:
        if used[i] == true:
            continue        // 该数已经选过，跳过
        
        used[i] = true
        path.append(nums[i])
        
        backtrack(path)    // 递归往下
        
        // 回溯：撤销选择
        path.pop()
        used[i] = false


主函数 permute(nums):
    result = []
    used = [false] * len(nums)
    backtrack(空列表)
    return result
```
回溯模板如下
```python
def backtrack(path):
    if 终止条件:
        保存结果
        return
    
    for 所有可选选项:
        if 这个选项不能选:   # ✂️剪枝
            continue
        
        做出选择
        backtrack(path)
        撤销选择   # 回溯！回到上一层，换另一个选项
```
## 七十八、子集

nums元素互不相同；返回全部子集（幂集）；子集无序，[1,2]与[2,1]算同一个，不能同时出现。 提供两种独立解法：for‑回溯(多叉树)、选‑不选(二叉树)

---
方法一：for循环回溯（推荐）
思路
1. 子集没有长度要求，每一条当前路径path都是一个合法子集，进入递归立刻存入答案。
2. 使用start变量：下一次只能从下标i+1往后选元素，禁止回头选取前面的数。
  - 选了1之后，后面只能选2、3
  - 选了2之后，后面只能选3
  - 彻底避免[1,2]、[2,1]这类重复子集
3. for循环遍历候选，做出选择 → 递归 → 回溯撤销选择。
伪代码
```text
result = 空列表
n = nums.length

// start：起始下标；path：当前子集
函数 backtrack(start, path):
    // 当前path就是子集，保存副本
    result.append( copy(path) )
    
    // 从start向后遍历，不回头
    for i = start ; i < n ; i++:
        path.append( nums[i] )        // 选nums[i]
        backtrack(i + 1, path)        // 下一轮只能选i之后
        path.pop()                     // 回溯，撤销选择


函数 subsets(nums):
    result = []
    backtrack(0, 空列表)
    return result

---
```
方法二：选 / 不选（二叉树 DFS）
思路
逐个处理每一个下标i的元素，两条分支：
1. 选 nums[i]，加入path，递归处理下一个元素i+1
2. 不选 nums[i]，path不变，直接递归处理下一个元素i+1
终止条件：当 i == n，所有元素处理完毕，此时path就是一个完整子集，加入结果。
天然不会生成乱序重复子集，因为严格按下标从左往右处理。
伪代码
```text
result = 空列表
n = nums.length

函数 dfs(i, path):
    // 所有元素抉择完毕，收集结果
    if i == n:
        result.append( copy(path) )
        return
    
    // 分支1：选择 nums[i]
    path.append(nums[i])
    dfs(i + 1, path)
    path.pop()
    
    // 分支2：不选择 nums[i]
    dfs(i + 1, path)


函数 subsets(nums):
    result = []
    dfs(0, 空列表)
    return result

---
```
两个回溯题关键点对比（子集 vs 全排列）
暂时无法在飞书文档外展示此内容
思路
n个元素，每个元素只有2种状态：选 / 不选。 所有子集等价于：枚举所有 n‑位二进制数（掩码mask）
- 第 i 位 = 1 → 选中 nums[i]
- 第 i 位 = 0 → 不选
掩码范围：0 ~ (2ⁿ‑1)
伪代码
```text
function subsets(nums):
    n = nums.length
    res = empty list
    total = 1 << n          // 2^n
    
    for mask from 0 to total‑1:
        cur = empty list
        for i from 0 to n‑1:
            if (mask & (1 << i)) != 0:
                cur.append(nums[i])
        res.append(cur)
    return res
```
## 十七、电话号码的字母组合
index：当前处理到第几个数字
path：当前拼接出来的字符串
```text
map = {
    '2':"abc",
    '3':"def",
    '4':"ghi",
    '5':"jkl",
    '6':"mno",
    '7':"pqrs",
    '8':"tuv",
    '9':"wxyz"
}
result = 空列表

函数 backtrack(index, path):
    if index == digits.length:
        result.append(path)
        return
    
    digit = digits[index]
    letters = map[digit]
    for c in letters:
        backtrack(index + 1, path + c)   // path+c生成新字符串，无需pop回溯

主函数 letterCombinations(digits):
    if digits == "":
        return []
    result = []
    backtrack(0, "")
    return result
```
## 三十九、组合总和
思路（回溯）
用 start 下标控制只能往后选（包含自己），避免出现乱序重复组合。
- 可以重复选当前元素：下一层递归起点还是 i，不是 i+1！（这是和子集题最大区别）
- 当前总和 > target：停止这条分支（剪枝）
变量：
- start：本轮从哪个下标开始取
- path：当前选中的组合
- sum：当前 path 的和
终止条件：
- sum == target → 保存 path，返回
- sum > target → 返回
伪代码
```text
res = 空列表

函数 backtrack(start, path, curSum):
    if curSum == target:
        res.append( copy(path) )
        return
    if curSum > target:
        return
    
    for i 从 start 到 len(candidates)-1:
        num = candidates[i]
        path.append(num)
        // 可以重复选，下一轮起点还是 i
        backtrack(i, path, curSum + num)
        path.pop()   //回溯撤销选择


主函数 combinationSum(candidates, target):
    res = []
    backtrack(0, [], 0)
    return res
```
## 二十二、括号生成
回溯思路
两个变量记录当前左、右括号用了多少个：
- left：已经选了几个左括号 (
- right：已经选了几个右括号 )
选择规则：
1. 左括号没用到 n 个：可以选左括号 (
2. 右括号数量 &lt; 左括号数量：才可以选右括号 )
终止条件：left == n && right == n，得到一条合法答案，保存。
伪代码
```text
res = 空列表

函数 backtrack(left, right, path):
    if left == n 并且 right == n:
        res.append( copy(path) )
        return
    
    // 尝试放左括号
    if left < n:
        path.append('(')
        backtrack(left+1, right, path)
        path.pop()
    
    // 尝试放右括号
    if right < left:
        path.append(')')
        backtrack(left, right+1, path)
        path.pop()


主函数 generateParenthesis(n):
    res = []
    backtrack(0, 0, [])
    return res
```


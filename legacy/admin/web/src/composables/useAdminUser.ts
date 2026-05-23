import { inject, provide, type Ref } from 'vue'

const KEY = Symbol('adminUserId')

export function provideAdminUser(userId: Ref<string>) {
  provide(KEY, userId)
}

export function useAdminUserId(): Ref<string> {
  const v = inject<Ref<string>>(KEY)
  if (!v) {
    throw new Error('admin user_id not provided')
  }
  return v
}

export function withUserQuery(params: URLSearchParams, userId: string) {
  params.set('user_id', userId.trim())
  return params
}

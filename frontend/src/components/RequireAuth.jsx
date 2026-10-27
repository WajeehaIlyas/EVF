import { useRouter } from 'next/router';
import { useEffect, useState } from 'react';

import { getAuth } from '../lib/auth';

/** Wrap a page: signed-out visitors are sent to /login and come back here afterwards. */
export default function RequireAuth({ children }) {
  const router = useRouter();
  const [ok, setOk] = useState(false);

  useEffect(() => {
    if (!router.isReady) return;
    if (getAuth()) setOk(true);
    else router.replace(`/login?next=${encodeURIComponent(router.asPath)}`);
  }, [router, router.isReady, router.asPath]);

  return ok ? children : null;
}

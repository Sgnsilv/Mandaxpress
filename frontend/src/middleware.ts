import { NextResponse, type NextRequest } from "next/server";

/**
 * Redirecionamento de CONVENIÊNCIA: sem cookie de sessão, vai direto ao login
 * (evita piscar a tela protegida). A segurança real é a validação no backend
 * em cada chamada de API; este arquivo não autoriza nada.
 */
export function middleware(request: NextRequest) {
  if (!request.cookies.has("mx_session")) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = "";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  // Tudo, exceto login, API, assets do Next e ícones.
  matcher: ["/((?!login|api|_next/static|_next/image|icon.svg|favicon.ico).*)"],
};

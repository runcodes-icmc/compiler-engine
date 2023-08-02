n = int(input("n: "))
n = 2 ** n - 1

print("\nNúmeros primos a partir de %d:" % n)
cont = 0
while cont < 5:
    EhPrimo = True
    # Tenta dividir n por x = 3 até raiz( n )+1, pegando só os ímpares
    x = 3
    limite = int(n ** 0.5)
    while EhPrimo and x <= limite:
        if n % x == 0:
            EhPrimo = False
        x = x + 2
    if EhPrimo:
        print(n, ", ", sep="", end="")
        cont = cont + 1
    n = n + 2

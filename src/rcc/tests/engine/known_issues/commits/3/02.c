#include<stdio.h>
#include<stdlib.h>

#define DIM 16

typedef struct no {
    char elem;
    char elemPossiveis[DIM];
    int qtdElemPossiveis;
} celula;

int removeElemPossivel(celula tabuleiro[DIM][DIM], int l, int c, char elem) {
    int i, j;

    if (tabuleiro[l][c].qtdElemPossiveis <= 1) {
        return 0;
    }
    for (i = 0; i < DIM; i++) { // itera na lista de elementos possiveis
        if (tabuleiro[l][c].elemPossiveis[i] == elem) { // verifica se o elemento passado estah na lista dos possiveis
            tabuleiro[l][c].elemPossiveis[i] = '.';
            tabuleiro[l][c].qtdElemPossiveis--;
            if (tabuleiro[l][c].qtdElemPossiveis == 1) {
                for (j = 0; j < DIM; j++) {
                    if (tabuleiro[l][c].elemPossiveis[j] != '.') {
                        //printf("%c ", tabuleiro[l][c].elemPossiveis[j]);
                        tabuleiro[l][c].elem = tabuleiro[l][c].elemPossiveis[j];
                        return 1;
                    }
                }
            }
            return 0;
        }
    }
    return 0;
}

int verificaLinha(celula tab[DIM][DIM], int l, int c) {
    int j;
    int qtdDescobertos = 0;

//    printf("Antes: ");
//    for (j = 0; j < DIM; j++) {
//        printf("%c ", tab[l][c].elemPossiveis[j]);
//    }
    for (j = 0; j < DIM; j++) { // iterando na linha
        if (tab[l][j].elem != '.') {
            qtdDescobertos = qtdDescobertos + removeElemPossivel(tab, l, c, tab[l][j].elem);
        }
    }
//    printf("\nDepois: ");
//    for (j = 0; j < DIM; j++) {
//        printf("%c ", tab[l][c].elemPossiveis[j]);
//    }
//    printf("\n-------------");
//    exit(0);
    return qtdDescobertos;
}

int verificaColuna(celula tab[DIM][DIM], int l, int c) {
    int j;
    int qtdDescobertos = 0;

//    printf("Antes: ");
//    for (j = 0; j < DIM; j++) {
//        printf("%c ", tab[l][c].elemPossiveis[j]);
//    }

    for (j = 0; j < DIM; j++) { // iterando na coluna
        if (tab[j][c].elem != '.') {
            qtdDescobertos = qtdDescobertos + removeElemPossivel(tab, l, c, tab[j][c].elem);
        }
    }
//    printf("\nDepois: ");
//    for (j = 0; j < DIM; j++) {
//        printf("%c ", tab[l][c].elemPossiveis[j]);
//    }
//    exit(0);
    //printf("\n-------------");

    return qtdDescobertos;
}

int verificaBloco(celula tab[DIM][DIM], int l, int c) {
    int j, k;
    int qtdDescobertos = 0;
    int inicioBlocoLinha = (l / 4) * 4;
    int inicioBlocoColuna = (c / 4) * 4;

    for (j = inicioBlocoLinha; j <= inicioBlocoLinha + (DIM / 4); j++) {
        for (k = inicioBlocoColuna; k <= inicioBlocoColuna + (DIM / 4); k++) {
            if (tab[j][k].elem != '.') {
                qtdDescobertos = qtdDescobertos + removeElemPossivel(tab, l, c, tab[j][k].elem);
            }
        }
    }

    return qtdDescobertos;
}

int preencherTabuleiro(celula tab[DIM][DIM], int n) {
    int i, j;

    while (n < DIM * DIM) {
        for (i = 0; i < DIM; i++) {
            for (j = 0; j < DIM; j++) {
                if (tab[i][j].elem == '.') {
                    n = n + verificaLinha(tab, i, j);
                    n = n + verificaColuna(tab, i, j);
                    n = n + verificaBloco(tab, i, j);
                }
            }
        }
        printf("%d\n", n);
    }
    return 1;
}

int lerMat(char path[32], celula tabuleiro[DIM][DIM]) {
    FILE *arqEntrada;
    char linha[DIM];
    char elemPossiveis[] = {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'A', 'B', 'C', 'D', 'E', 'F'};
    int i, j, k;
    int n = 0;

    arqEntrada = fopen(path, "rb");
    if (arqEntrada == NULL) {
        printf("Problema ao abrir arquivo %s", path);
        return 0;
    }

    for (i = 0; i < DIM; i++) {
        fscanf(arqEntrada, "%s", linha);
        for (j = 0; j < DIM; j++) {
            tabuleiro[i][j].elem = linha[j];
            if (tabuleiro[i][j].elem != '.') {
                tabuleiro[i][j].qtdElemPossiveis = 1;
                n++;
            } else {
                elemPossiveis[0] = '0';
                for (k = 0; k < DIM; k++) {
                    tabuleiro[i][j].elemPossiveis[k] = elemPossiveis[k];
                }
                tabuleiro[i][j].qtdElemPossiveis = DIM;
            }
        }
    }
    fclose(arqEntrada);
    return n;
}

int imprimirMat(celula mat[DIM][DIM]) {
    int i, j;

    for (i = 0; i < DIM; i++) {
        for (j = 0; j < DIM; j++) {
            printf("%c", mat[i][j].elem);
        }
        printf("\n");
    }
    return 1;
}

int main() {
    char path[32];
    celula tabuleiro[DIM][DIM];
    //int n = 0;  //qtd de elementos preenchidos;

    scanf("%s", path);
    //n =
    lerMat(path, tabuleiro);
   // preencherTabuleiro(tabuleiro, n);
    imprimirMat(tabuleiro);


    return 1;
}
